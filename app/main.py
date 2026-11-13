"""HTTP server: REST API + web interface.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import html
import json
import logging
import os
import sqlite3
import time
from collections import defaultdict
from pathlib import Path
from typing import Literal

from fastapi import Cookie, Depends, FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import (
    alternates,
    auth,
    captcha,
    datasheet,
    datasheet_ai,
    db,
    eccn,
    editors,
    exemptions,
    history,
    mailer,
    manufacturers,
    svhc,
)
from .analyzer import analyze, parse_classifications
from .importer import IMPORT_FIELDS, detect_import_columns, import_table, plan_table
from .reports import database_csv, report_csv, report_xlsx
from .tables import (
    BOM_FIELDS,
    HEADER_ROW_KEY,
    TableError,
    apply_substitutions,
    bom_lines,
    detect_columns,
    modified_bom_csv,
    parse_bom,
    read_rows,
    status_choices,
    status_value_summary,
    table_preview,
)

STATIC = Path(__file__).resolve().parent / "static"
EXAMPLES = Path(__file__).resolve().parent.parent / "examples"   # example BOMs, offered on the home page
MAX_UPLOAD = 10 * 1024 * 1024  # 10 MB
MAX_DATASHEET = 30 * 1024 * 1024  # 30 MB

app = FastAPI(
    title="RoHS / REACH Checker",
    description="RoHS 2, RoHS 3 and REACH compliance analysis of a bill of materials (BOM) against a component database.",
    version="2.0.0",
)
log = logging.getLogger("rohs_checker")

# Errors on the server side: a JSON answer the interface understands (X-Error-Kind), so that it can tell the user
# that the site is not working, instead of a bare "Internal Server Error".
DATABASE_DOWN = "The database is not available: the site is not working at the moment."
SERVER_ERROR = "Server error: the site is not working properly at the moment."


@app.exception_handler(sqlite3.Error)
async def database_unavailable(request: Request, exc: sqlite3.Error):
    log.error("Database error on %s %s", request.method, request.url.path, exc_info=exc)
    return JSONResponse({"detail": DATABASE_DOWN}, status_code=503, headers={"X-Error-Kind": "database"})


@app.exception_handler(Exception)
async def server_error(request: Request, exc: Exception):
    # Starlette also logs the traceback of these errors
    return JSONResponse({"detail": SERVER_ERROR}, status_code=500, headers={"X-Error-Kind": "server"})


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """Force the browser to revalidate the page and its scripts on every load.

    Otherwise a browser may keep an old app.js in cache with a recent index.html,
    and the table columns no longer match the headers.
    """
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-cache"
    return response


def get_conn():
    conn = db.connect()
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


def current_user(rrc_session: str | None = Cookie(default=None), conn=Depends(get_conn)) -> dict | None:
    """Signed-in user (session cookie), or None."""
    return auth.user_from_token(conn, rrc_session)


def require_user(user=Depends(current_user)) -> dict:
    if not user:
        raise HTTPException(401, "Sign-in required")
    return user


def require_editor(user=Depends(current_user)) -> dict | None:
    """Database changes: the administrator or an account it approved (anyone with RRC_OPEN_EDITING)."""
    if editors.open_editing():
        return user
    if not user:
        raise HTTPException(401, "Sign in to change the database")
    if not editors.can_edit(user):
        raise HTTPException(403, "Your account cannot change the database yet: request edit access in the Database tab")
    return user


def require_admin(user=Depends(current_user)) -> dict | None:
    """Deleting components: the administrator only (anyone with RRC_OPEN_EDITING)."""
    if editors.open_editing():
        return user
    if not user:
        raise HTTPException(401, "Sign in to change the database")
    if not editors.is_admin(user):
        raise HTTPException(403, "Only the administrator can delete components")
    return user


def check_additions(conn, user: dict | None, count: int) -> None:
    try:
        editors.check_additions(conn, user, count)
    except editors.LimitReached as exc:
        # The error cancels the transaction: nothing added by this request is kept
        raise HTTPException(429, str(exc)) from exc
    editors.record_additions(conn, user, count)


class ComponentIn(BaseModel):
    mpn: str
    manufacturer: str = ""
    description: str = ""
    rohs2: str = "unknown"
    rohs3: str = "unknown"
    reach: str = "unknown"
    rohs_exemptions: str = ""
    svhc_list: str = ""
    eccn: str = ""
    country_of_origin: str = ""
    source: str = ""
    last_checked: str = ""


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File too large (max 10 MB)")
    if not data:
        raise HTTPException(400, "Empty file")
    return data


MAPPING_DOC = ('Column mapping, JSON: {"mpn": "Part Number", "manufacturer": "__none__", "_header_row": 3}. '
               '"__none__" ignores a field; "_header_row" is the number of the header row (0 = no header).')
# Response header that flags a column error: the interface then opens the column mapping dialog.
COLUMNS_ERROR = {"X-Error-Kind": "columns"}


def _parse_mapping(mapping: str) -> dict | None:
    if not mapping:
        return None
    try:
        parsed = json.loads(mapping)
    except ValueError:
        parsed = None
    if not isinstance(parsed, dict):
        raise HTTPException(400, "mapping parameter: a JSON object is expected")
    return parsed


# ---------------------------------------------------------------- Preview and column mapping

def _bom_value(line, role: str) -> str:
    """Value of a field as the analysis will understand it (normalised statuses when possible)."""
    if role == "dnp":
        return "yes" if line.dnp else ""
    value = getattr(line, role)
    if role in ("rohs2", "rohs3", "reach") and value:
        try:
            return db.clean_status(value, db.REACH_STATUSES if role == "reach" else db.ROHS_STATUSES, role)
        except db.ValidationError:
            return value  # unrecognised: shown as is (the analysis will flag it)
    return value


def _bom_result(conn, rows, mapping, sample: int = 10) -> dict:
    """BOM lines as they will be read, and whether each part number is in the database."""
    lines = bom_lines(rows, mapping) if "mpn" in mapping.columns else []
    roles = [k for k, _, _ in BOM_FIELDS if k in mapping.columns]

    def in_base(mpn: str) -> bool:
        return bool(mpn) and conn.execute(
            "SELECT 1 FROM components WHERE mpn_norm = ? OR mpn_loose = ? LIMIT 1", (db.normalize_mpn(mpn), db.loose_mpn(mpn))
        ).fetchone() is not None

    found = [in_base(line.mpn) for line in lines]
    detail = [
        {
            "line": line.line,
            "values": {r: _bom_value(line, r) for r in roles},
            "in_base": ok,
        }
        for line, ok in list(zip(lines, found, strict=True))[:sample]
    ]
    return {"counts": {"lines": len(lines), "in_base": sum(found), "dnp": sum(line.dnp for line in lines)}, "lines": detail}


@app.post("/api/table/preview", summary="Preview a CSV/XLSX file, its column mapping and the result")
async def preview_table(
    file: UploadFile = File(...),
    kind: Literal["bom", "components"] = Query(default="bom", description="bom: BOM analysis; components: database import"),
    mapping: str = Form(default="", description=MAPPING_DOC),
    conn=Depends(get_conn),
):
    """Returns, in addition to the raw preview:

    - detected: the mapping proposed by automatic detection (to highlight manual choices);
    - result: the first lines as they will be interpreted and a summary of the whole file.
      BOM: whether each part number is in the database. Import: creation, change (old → new value),
      unchanged row or error, without writing anything.
    """
    data = await _read_upload(file)
    overrides = _parse_mapping(mapping)
    try:
        rows = read_rows(data, file.filename or "")
        if kind == "bom":
            m = detect_columns(rows, overrides, require_mpn=False)
            preview = table_preview(rows, m.header_row, m.labels, m.columns, BOM_FIELDS)
            auto = detect_columns(rows, {HEADER_ROW_KEY: m.header_row + 1}, require_mpn=False).columns
            result = _bom_result(conn, rows, m)
            values = status_value_summary(rows, m.header_row, m.columns, m.status_values)
        else:
            hr, labels, columns = detect_import_columns(rows, overrides)
            preview = table_preview(rows, hr, labels, columns, IMPORT_FIELDS)
            auto = detect_import_columns(rows, {HEADER_ROW_KEY: hr + 1})[2]
            choices = status_choices(overrides)
            result = plan_table(conn, rows, hr, columns, choices=choices) if "mpn" in columns else {"counts": {}, "lines": []}
            values = status_value_summary(rows, hr, columns, choices)
    except TableError as exc:
        raise HTTPException(422, str(exc)) from exc
    detected = {role: preview["headers"][idx] for role, idx in auto.items()}
    return {"kind": kind, "filename": file.filename or "", **preview, "detected": detected, "result": result,
            "status_values": values}


# ---------------------------------------------------------------- BOM analysis

@app.post("/api/analyze", summary="Analyse a BOM (CSV, TXT or XLSX)")
async def analyze_bom(
    file: UploadFile = File(...),
    mapping: str = Form(default="", description=MAPPING_DOC),
    substitutions: str = Form(default="", description='Replacements, JSON: {"<line number>": <component id>, ...}'),
    classifications: str = Form(default="", description='Statuses given by hand to lines that need review, JSON: '
                                '{"<line number>": {"rohs3": "compliant", ...}}. Analysis only: the database is unchanged.'),
    format: Literal["json", "xlsx", "csv", "bom"] = Query(default="json", description="bom: the modified bill of materials (replacements applied), as CSV"),
    save: str = Form(default="", description="1: save the analysis in the signed-in user's history"),
    history_id: str = Form(default="", description="History entry to update (same BOM, changed settings)"),
    conn=Depends(get_conn),
    user=Depends(current_user),
):
    data = await _read_upload(file)
    overrides = _parse_mapping(mapping)
    try:
        subs = {int(k): int(v) for k, v in (json.loads(substitutions) if substitutions else {}).items()}
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "substitutions parameter: invalid JSON") from None
    try:
        manual = parse_classifications(json.loads(classifications)) if classifications else {}
    except ValueError as exc:
        raise HTTPException(400, f"classifications parameter: {exc}") from None
    try:
        lines, colmap = parse_bom(data, file.filename or "", overrides)
    except TableError as exc:
        raise HTTPException(422, str(exc), headers=COLUMNS_ERROR) from exc
    try:
        replaced = apply_substitutions(conn, lines, subs)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    stem = Path(file.filename or "bom").stem
    if format == "bom":
        return Response(
            modified_bom_csv(data, file.filename or "", overrides, colmap, replaced),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{stem}_modified.csv"'},
        )
    report = analyze(conn, lines, colmap, file.filename or "", manual)
    if format == "xlsx":
        return Response(
            report_xlsx(report),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="compliance_{stem}.xlsx"'},
        )
    if format == "csv":
        return Response(
            report_csv(report),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="compliance_{stem}.csv"'},
        )
    _count_line_equivalents(conn, report["lines"])
    if user and (save == "1" or history_id):
        report["history_id"] = history.save(
            conn, user["id"], file.filename or "bom", data, mapping, substitutions, report,
            history_id=int(history_id) if history_id.isdigit() else None, classifications=classifications,
        )
    return report


def _count_line_equivalents(conn, lines: list[dict]) -> None:
    """Number of equivalents the Equivalents button of each line would list."""
    counter = alternates.EquivalentCounter(conn)
    for line in lines:
        comp = line["component"]
        if line["dnp"] or not line["bom_mpn"]:
            line["equivalents"] = None
        elif comp:
            line["equivalents"] = counter.count(description=comp["description"], mpn=comp["mpn"],
                                                manufacturer=comp["manufacturer"], component_id=comp["id"])
        else:
            line["equivalents"] = counter.count(description=line["bom_description"], mpn=line["bom_mpn"],
                                                manufacturer=line["bom_manufacturer"])


# ---------------------------------------------------------------- User accounts

class RegisterIn(BaseModel):
    email: str
    password: str
    name: str = ""
    captcha: str = ""   # Turnstile token, when the captcha is on


class LoginIn(BaseModel):
    email: str
    password: str


class ConfirmIn(BaseModel):
    token: str
    password: str


# Password attempt throttling: 5 failures per address in 15 minutes
_LOGIN_WINDOW, _LOGIN_MAX = 15 * 60, 5
_login_failures: dict[str, list[float]] = defaultdict(list)


def _check_throttle(email: str) -> None:
    now = time.monotonic()
    recent = [t for t in _login_failures[email] if now - t < _LOGIN_WINDOW]
    _login_failures[email] = recent
    if len(recent) >= _LOGIN_MAX:
        raise HTTPException(429, "Too many attempts: try again in a few minutes")


def _account(conn, user: dict | None) -> dict | None:
    """Signed-in user with their rights on the database."""
    if not user:
        return None
    return user | {"is_admin": editors.is_admin(user), "can_edit": editors.can_edit(user),
                   "additions_left": editors.additions_left(conn, user), "daily_additions": editors.daily_limit()}


def _open_session(conn, user: dict, request: Request, response: Response) -> dict:
    token = auth.create_session(conn, user["id"])
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )
    return _account(conn, user)


# Sign-ups per client address while confirmation e-mails are sent: 10 per hour
_SIGNUP_WINDOW, _SIGNUP_MAX = 60 * 60, 10
_signup_attempts: dict[str, list[float]] = defaultdict(list)


def _check_signup_throttle(ip: str) -> None:
    now = time.monotonic()
    recent = [t for t in _signup_attempts[ip] if now - t < _SIGNUP_WINDOW]
    if len(recent) >= _SIGNUP_MAX:
        _signup_attempts[ip] = recent
        raise HTTPException(429, "Too many sign-ups from your connection: try again later")
    _signup_attempts[ip] = [*recent, now]


def _public_url(request: Request) -> str:
    """Base address for the links in e-mails (Render sets RENDER_EXTERNAL_URL)."""
    base = os.environ.get("RRC_PUBLIC_URL") or os.environ.get("RENDER_EXTERNAL_URL") or str(request.base_url)
    return base.rstrip("/")


def _confirmation_email(link: str) -> str:
    # The name typed in the form is left out on purpose: a sign-up form must not let anyone
    # send their own text to someone else's mailbox.
    url = html.escape(link, quote=True)
    return (
        "<p>Hello,</p>"
        "<p>An account was created with this e-mail address on the RoHS / REACH Checker. To confirm it, "
        "open the link below and enter the password chosen when creating the account.</p>"
        f'<p><a href="{url}">Confirm my account</a></p>'
        f"<p>Or copy this address into your browser:<br>{url}</p>"
        f"<p>The link is valid for {auth.SIGNUP_HOURS} hours. If you did not create this account, "
        "ignore this e-mail: no account is created without confirmation.</p>"
    )


@app.get("/api/auth/config", summary="Sign-up settings: e-mail confirmation, captcha key")
def auth_config():
    return {"email_confirmation": mailer.available(), "captcha_site_key": captcha.site_key(),
            "open_editing": editors.open_editing(), "edit_requests": bool(editors.admin_email() and mailer.available()),
            "daily_additions": editors.daily_limit()}


@app.post("/api/auth/register", status_code=201,
          summary="Create an account: signed in at once, or 202 when a confirmation e-mail is sent")
def register(body: RegisterIn, request: Request, response: Response, conn=Depends(get_conn)):
    ip = request.client.host if request.client else ""
    confirm_by_email = mailer.available()
    if confirm_by_email:
        _check_signup_throttle(ip)
    if not captcha.verify(body.captcha, ip or None):
        raise HTTPException(400, "Captcha check failed: try again")
    try:
        if not confirm_by_email:
            return _open_session(conn, auth.create_user(conn, body.email, body.password, body.name), request, response)
        token = auth.create_signup_request(conn, body.email, body.password, body.name)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except auth.TooManyRequests as exc:
        raise HTTPException(429, str(exc)) from exc
    except auth.AuthError as exc:
        raise HTTPException(409, str(exc)) from exc
    email = auth.normalize_email(body.email)
    try:
        mailer.send(email, "Confirm your account - RoHS / REACH Checker",
                    _confirmation_email(f"{_public_url(request)}/#confirm/{token}"))
    except mailer.MailError as exc:
        # The error cancels the transaction: the sign-up attempt is not kept
        raise HTTPException(502, "The confirmation e-mail could not be sent: try again later") from exc
    response.status_code = 202
    return {"pending": True, "email": email, "hours": auth.SIGNUP_HOURS}


@app.post("/api/auth/confirm", summary="Confirm a sign-up with the token of the e-mailed link (and sign in)")
def confirm(body: ConfirmIn, request: Request, response: Response, conn=Depends(get_conn)):
    try:
        user = auth.confirm_signup(conn, body.token, body.password)
    except auth.AuthError as exc:
        raise HTTPException(400, str(exc)) from exc
    return _open_session(conn, user, request, response)


@app.post("/api/auth/login", summary="Sign in")
def login(body: LoginIn, request: Request, response: Response, conn=Depends(get_conn)):
    email = auth.normalize_email(body.email)
    _check_throttle(email)
    try:
        user = auth.authenticate(conn, email, body.password)
    except auth.AuthError as exc:
        if auth.signup_pending(conn, email, body.password):
            raise HTTPException(403, "Confirm your e-mail address first: open the link sent to you "
                                     "when you created the account") from exc
        _login_failures[email].append(time.monotonic())
        raise HTTPException(401, str(exc)) from exc
    _login_failures.pop(email, None)
    return _open_session(conn, user, request, response)


@app.post("/api/auth/logout", status_code=204, summary="Sign out")
def logout(response: Response, rrc_session: str | None = Cookie(default=None), conn=Depends(get_conn)):
    auth.delete_session(conn, rrc_session)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@app.get("/api/auth/me", summary="Signed-in user and their rights on the database (null if nobody)")
def me(user=Depends(current_user), conn=Depends(get_conn)):
    return _account(conn, user)


# ---------------------------------------------------------------- edit access, approved by the administrator

class TokenIn(BaseModel):
    token: str


class ReviewIn(BaseModel):
    token: str
    approve: bool


def _request_email(email: str, created_at: str, link: str) -> str:
    url = html.escape(link, quote=True)
    return (
        "<p>Hello,</p>"
        f"<p><strong>{html.escape(email)}</strong> (account created on {html.escape(created_at[:10])}) asks for "
        "permission to add and edit components in the database of the RoHS / REACH Checker.</p>"
        f'<p><a href="{url}">Review the request</a> (approve or refuse)</p>'
        f"<p>Or copy this address into your browser:<br>{url}</p>"
        f"<p>The link is valid for {editors.REVIEW_DAYS} days. An approved account can add up to "
        f"{editors.daily_limit()} new components per day; only you can delete components.</p>"
    )


def _decision_email(approved: bool, site: str) -> str:
    url = html.escape(site, quote=True)
    if approved:
        text = ("Your request to edit the database of the RoHS / REACH Checker was approved: you can now add "
                f"and edit components (up to {editors.daily_limit()} new components per day).")
    else:
        text = "Your request to edit the database of the RoHS / REACH Checker was not approved."
    return f'<p>Hello,</p><p>{text}</p><p><a href="{url}">{url}</a></p>'


@app.post("/api/edit-access/request", summary="Ask the administrator for the right to change the database")
def request_edit_access(request: Request, user=Depends(require_user), conn=Depends(get_conn)):
    admin = editors.admin_email()
    if not (admin and mailer.available()):
        raise HTTPException(503, "Edit access requests are not set up on this server")
    try:
        token = editors.request_access(conn, user)
    except editors.EditAccessError as exc:
        raise HTTPException(409, str(exc)) from exc
    try:
        mailer.send(admin, "Edit access request - RoHS / REACH Checker",
                    _request_email(user["email"], user["created_at"], f"{_public_url(request)}/#review/{token}"))
    except mailer.MailError as exc:
        # The error cancels the transaction: the request is not kept
        raise HTTPException(502, "The request could not be sent to the administrator: try again later") from exc
    return _account(conn, user | {"edit_access": "pending"})


@app.post("/api/edit-access/lookup", summary="Request matching an administrator's review link")
def lookup_edit_access(body: TokenIn, conn=Depends(get_conn)):
    row = editors.find_request(conn, body.token)
    if not row:
        raise HTTPException(404, "This link is invalid, has expired or was already used")
    return {"email": row["email"], "name": row["name"], "created_at": row["created_at"],
            "requested_at": row["edit_requested_at"]}


@app.post("/api/edit-access/review", summary="Approve or refuse a request (token of the administrator's link)")
def review_edit_access(body: ReviewIn, request: Request, conn=Depends(get_conn)):
    try:
        row = editors.review(conn, body.token, body.approve)
    except editors.EditAccessError as exc:
        raise HTTPException(404, str(exc)) from exc
    try:  # the user is told; a failure here does not cancel the decision
        mailer.send(row["email"], "Your edit access request - RoHS / REACH Checker",
                    _decision_email(body.approve, _public_url(request) + "/#database"))
    except mailer.MailError:
        pass
    return {"email": row["email"], "edit_access": row["edit_access"]}


# ---------------------------------------------------------------- BOM history

@app.get("/api/history", summary="Latest BOMs analysed by the signed-in user")
def list_history(user=Depends(require_user), conn=Depends(get_conn)):
    return history.list_for(conn, user["id"])


@app.get("/api/history/{history_id}/file", summary="Original file of a BOM from the history")
def history_file(history_id: int, user=Depends(require_user), conn=Depends(get_conn)):
    found = history.get_file(conn, user["id"], history_id)
    if not found:
        raise HTTPException(404, "Analysis not found")
    filename, data = found
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename) or "bom"
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@app.delete("/api/history/{history_id}", status_code=204, summary="Delete a BOM from the history")
def delete_history(history_id: int, user=Depends(require_user), conn=Depends(get_conn)):
    if not history.delete(conn, user["id"], history_id):
        raise HTTPException(404, "Analysis not found")
    return Response(status_code=204)


# ---------------------------------------------------------------- Component database

@app.get("/api/components", summary="Search components")
def list_components(
    q: str = "",
    status: str = Query(default="", description="'issues' to show only non-compliant / unknown components"),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn=Depends(get_conn),
):
    items, total = db.search(conn, q, limit, offset, status)
    return {"total": total, "items": alternates.EquivalentCounter(conn).annotate(items)}


@app.get("/api/components/export", summary="Export the whole database as CSV")
def export_components(conn=Depends(get_conn)):
    return Response(
        database_csv(db.all_components(conn)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="component_database.csv"'},
    )


@app.get("/api/alternatives", summary="Equivalents from other manufacturers for any part number (e.g. a BOM line not in the database)")
def alternatives_for(mpn: str = "", manufacturer: str = "", description: str = "", conn=Depends(get_conn)):
    return alternates.find(conn, mpn=mpn, manufacturer=manufacturer, description=description)


@app.get("/api/components/{comp_id}/alternatives", summary="Equivalents from other manufacturers for a database component")
def component_alternatives(comp_id: int, conn=Depends(get_conn)):
    if not db.get(conn, comp_id):
        raise HTTPException(404, "Component not found")
    return alternates.find(conn, component_id=comp_id)


class EquivalentIn(BaseModel):
    other_id: int


@app.post("/api/components/{comp_id}/equivalents", status_code=204, dependencies=[Depends(require_editor)],
          summary="Confirm that another component is equivalent")
def add_equivalent(comp_id: int, body: EquivalentIn, conn=Depends(get_conn)):
    try:
        alternates.confirm(conn, comp_id, body.other_id)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(status_code=204)


@app.delete("/api/components/{comp_id}/equivalents/{other_id}", status_code=204, dependencies=[Depends(require_editor)],
            summary="Remove a confirmed equivalence")
def remove_equivalent(comp_id: int, other_id: int, conn=Depends(get_conn)):
    alternates.unconfirm(conn, comp_id, other_id)
    return Response(status_code=204)


@app.get("/api/components/{comp_id}/sheet",
         summary="A component's page: its record and the details of its regulations (exemptions, SVHC, ECCN)")
def component_sheet(comp_id: int, conn=Depends(get_conn)):
    comp = db.get(conn, comp_id)
    if not comp:
        raise HTTPException(404, "Component not found")
    alternates.EquivalentCounter(conn).annotate([comp])
    return {
        "component": comp,
        "exemptions": [exemptions.describe(c) for c in exemptions.split_codes(comp.get("rohs_exemptions", ""))],
        "eccn": eccn.describe(comp["eccn"]) if comp.get("eccn") else None,
        "svhc": svhc.match_text(comp.get("svhc_list", "")),
        "svhc_list": svhc.info(),  # whether the Candidate List is loaded, and its version
    }


@app.get("/api/svhc", summary="REACH Candidate List (SVHC) and the number of database components containing each substance")
def list_svhc(conn=Depends(get_conn)):
    return svhc.list_all(conn)


@app.get("/api/svhc/{sid}", summary="An SVHC substance of the Candidate List and the components that contain it")
def get_svhc(sid: str, conn=Depends(get_conn)):
    info = svhc.get(conn, sid)
    if info is None:
        raise HTTPException(404, "Substance not found in the Candidate List")
    alternates.EquivalentCounter(conn).annotate(info["components"])
    return info


@app.get("/api/components/{comp_id}")
def get_component(comp_id: int, conn=Depends(get_conn)):
    comp = db.get(conn, comp_id)
    if not comp:
        raise HTTPException(404, "Component not found")
    return comp


@app.post("/api/components", status_code=201)
def create_component(body: ComponentIn, user=Depends(require_editor), conn=Depends(get_conn)):
    try:
        cid = db.create(conn, body.model_dump())
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    check_additions(conn, user, 1)
    return db.get(conn, cid)


@app.put("/api/components/{comp_id}", dependencies=[Depends(require_editor)])
def update_component(comp_id: int, body: ComponentIn, conn=Depends(get_conn)):
    try:
        ok = db.update(conn, comp_id, body.model_dump())
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not ok:
        raise HTTPException(404, "Component not found")
    return db.get(conn, comp_id)


@app.delete("/api/components/{comp_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_component(comp_id: int, conn=Depends(get_conn)):
    if not db.delete(conn, comp_id):
        raise HTTPException(404, "Component not found")
    return Response(status_code=204)


@app.post("/api/components/import", summary="Import / update the database from a CSV or XLSX file")
async def import_components(
    file: UploadFile = File(...),
    mapping: str = Form(default="", description=MAPPING_DOC),
    user=Depends(require_editor),
    conn=Depends(get_conn),
):
    data = await _read_upload(file)
    overrides = _parse_mapping(mapping)
    try:
        result = import_table(conn, data, file.filename or "", overrides)
    except TableError as exc:
        raise HTTPException(422, str(exc), headers=COLUMNS_ERROR) from exc
    check_additions(conn, user, result["created"])
    return result


# ---------------------------------------------------------------- PDF datasheet

@app.post(
    "/api/datasheet/extract",
    summary="Extract a component's information from its PDF datasheet (proposal to review)",
    dependencies=[Depends(require_editor)],
)
def extract_datasheet(
    file: UploadFile = File(...),
    method: Literal["auto", "heuristic", "ai"] = Query(default="auto", description="auto: AI if an API key is configured, otherwise heuristic"),
    conn=Depends(get_conn),
):
    data = file.file.read(MAX_DATASHEET + 1)
    if len(data) > MAX_DATASHEET:
        raise HTTPException(413, "PDF too large (max 30 MB)")
    if not data:
        raise HTTPException(400, "Empty file")
    name = file.filename or "datasheet.pdf"

    try:
        heur = datasheet.extract(data, name)
    except datasheet.DatasheetError as exc:
        raise HTTPException(422, str(exc)) from exc

    use_ai = method == "ai" or (method == "auto" and datasheet_ai.available())
    if method == "ai" and not datasheet_ai.available():
        raise HTTPException(400, "AI extraction unavailable: no ANTHROPIC_API_KEY configured on the server")
    result = heur
    if use_ai:
        try:
            result = datasheet_ai.extract(data, name)
            # Fill empty fields from the heuristic
            for key, val in heur["fields"].items():
                if not result["fields"].get(key) and val:
                    result["fields"][key] = val
            if not result["candidates"]["manufacturer"]:
                result["candidates"]["manufacturer"] = heur["candidates"]["manufacturer"]
            result["pages"] = heur["pages"]
        except datasheet_ai.AIExtractionError as exc:
            heur["warnings"].append(f"AI extraction failed ({exc}): heuristic result shown")

    # Does the component already exist?
    f = result["fields"]
    existing = None
    if f.get("mpn"):
        row = conn.execute(
            "SELECT id FROM components WHERE mpn_norm = ? AND manufacturer_norm = ?",
            (db.normalize_mpn(f["mpn"]), db.normalize_manufacturer(f.get("manufacturer", ""))),
        ).fetchone()
        existing = row["id"] if row else None
    result["existing_id"] = existing
    result["ai_available"] = datasheet_ai.available()
    result.pop("manufacturer_norm", None)
    return result


# ---------------------------------------------------------------- Manufacturers
# Key = normalised manufacturer name; "_" stands for components without a manufacturer.

class ManufacturerIn(BaseModel):
    name: str = ""
    website: str = ""
    country: str = ""
    notes: str = ""


def _mfr_key(key: str) -> str:
    return "" if key == "_" else key


@app.get("/api/manufacturers", summary="List of manufacturers with their statistics")
def list_manufacturers(conn=Depends(get_conn)):
    return manufacturers.list_all(conn)


@app.get("/api/manufacturers/{key}", summary="A manufacturer's record and its components")
def get_manufacturer(key: str, conn=Depends(get_conn)):
    info = manufacturers.get(conn, _mfr_key(key))
    if info is None:
        raise HTTPException(404, "Manufacturer not found")
    alternates.EquivalentCounter(conn).annotate(info["components"])
    return info


@app.put("/api/manufacturers/{key}", dependencies=[Depends(require_editor)], summary="Edit a manufacturer's information")
def update_manufacturer(key: str, body: ManufacturerIn, conn=Depends(get_conn)):
    try:
        return manufacturers.update(conn, _mfr_key(key), body.model_dump())
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------- RoHS exemptions

class ExemptionNotesIn(BaseModel):
    notes: str = ""


@app.get("/api/exemptions", summary="RoHS exemptions used by the database components")
def list_exemptions(conn=Depends(get_conn)):
    return exemptions.list_all(conn)


@app.get("/api/exemptions/{code}", summary="A RoHS exemption's record: official text, dates, components")
def get_exemption(code: str, conn=Depends(get_conn)):
    info = exemptions.get(conn, code)
    alternates.EquivalentCounter(conn).annotate(info["components"])
    return info


@app.put("/api/exemptions/{code}", dependencies=[Depends(require_editor)], summary="Edit an exemption's notes")
def update_exemption(code: str, body: ExemptionNotesIn, conn=Depends(get_conn)):
    try:
        return exemptions.update(conn, code, body.notes)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------- ECCN codes

class EccnIn(BaseModel):
    title: str = ""
    notes: str = ""


@app.get("/api/eccn", summary="ECCN codes in the database, with their explanation")
def list_eccn(conn=Depends(get_conn)):
    return eccn.list_all(conn)


@app.get("/api/eccn/{code}", summary="An ECCN code's record: scope, official definition, components")
def get_eccn(code: str, conn=Depends(get_conn)):
    try:
        info = eccn.get(conn, code)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    alternates.EquivalentCounter(conn).annotate(info["components"])
    return info


@app.put("/api/eccn/{code}", dependencies=[Depends(require_editor)], summary="Edit an ECCN code's title and notes")
def update_eccn(code: str, body: EccnIn, conn=Depends(get_conn)):
    try:
        return eccn.update(conn, code, body.model_dump())
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/countries", summary="Recognised ISO country codes and their names")
def get_countries():
    return db.COUNTRIES


@app.get("/api/health", summary="Is the site working? Checks that the database answers (503 otherwise)")
def health(conn=Depends(get_conn)):
    conn.execute("SELECT 1 FROM components LIMIT 1").fetchone()
    return {"status": "ok"}


@app.get("/api/stats")
def get_stats(conn=Depends(get_conn)):
    return db.stats(conn) | {"ai_available": datasheet_ai.available()}


# ---------------------------------------------------------------- Web interface

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
app.mount("/examples", StaticFiles(directory=EXAMPLES, check_dir=False), name="examples")
