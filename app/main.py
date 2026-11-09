"""HTTP server: REST API + web interface.

Run with:  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Literal

from fastapi import Cookie, Depends, FastAPI, File, Form, Header, HTTPException, Query, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import alternates, auth, datasheet, datasheet_ai, db, eccn, exemptions, history, manufacturers
from .analyzer import analyze
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
    table_preview,
)

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 10 * 1024 * 1024  # 10 MB
MAX_DATASHEET = 30 * 1024 * 1024  # 30 MB
ADMIN_TOKEN = os.environ.get("RRC_ADMIN_TOKEN", "")

app = FastAPI(
    title="RoHS / REACH Checker",
    description="RoHS 2, RoHS 3 and REACH compliance analysis of a bill of materials (BOM) against a component database.",
    version="2.0.0",
)


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


def require_admin(x_admin_token: str = Header(default="")):
    """If RRC_ADMIN_TOKEN is set, database changes require the X-Admin-Token header."""
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "Admin token required (X-Admin-Token header)")


def current_user(rrc_session: str | None = Cookie(default=None), conn=Depends(get_conn)) -> dict | None:
    """Signed-in user (session cookie), or None."""
    return auth.user_from_token(conn, rrc_session)


def require_user(user=Depends(current_user)) -> dict:
    if not user:
        raise HTTPException(401, "Sign-in required")
    return user


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
        else:
            hr, labels, columns = detect_import_columns(rows, overrides)
            preview = table_preview(rows, hr, labels, columns, IMPORT_FIELDS)
            auto = detect_import_columns(rows, {HEADER_ROW_KEY: hr + 1})[2]
            result = plan_table(conn, rows, hr, columns) if "mpn" in columns else {"counts": {}, "lines": []}
    except TableError as exc:
        raise HTTPException(422, str(exc)) from exc
    detected = {role: preview["headers"][idx] for role, idx in auto.items()}
    return {"kind": kind, "filename": file.filename or "", **preview, "detected": detected, "result": result}


# ---------------------------------------------------------------- BOM analysis

@app.post("/api/analyze", summary="Analyse a BOM (CSV, TXT or XLSX)")
async def analyze_bom(
    file: UploadFile = File(...),
    mapping: str = Form(default="", description=MAPPING_DOC),
    substitutions: str = Form(default="", description='Replacements, JSON: {"<line number>": <component id>, ...}'),
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
    report = analyze(conn, lines, colmap, file.filename or "")
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
    if user and (save == "1" or history_id):
        report["history_id"] = history.save(
            conn, user["id"], file.filename or "bom", data, mapping, substitutions, report,
            history_id=int(history_id) if history_id.isdigit() else None,
        )
    return report


# ---------------------------------------------------------------- User accounts

class RegisterIn(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginIn(BaseModel):
    email: str
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


def _open_session(conn, user: dict, request: Request, response: Response) -> dict:
    token = auth.create_session(conn, user["id"])
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )
    return user


@app.post("/api/auth/register", status_code=201, summary="Create an account (and sign in)")
def register(body: RegisterIn, request: Request, response: Response, conn=Depends(get_conn)):
    try:
        user = auth.create_user(conn, body.email, body.password, body.name)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except auth.AuthError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _open_session(conn, user, request, response)


@app.post("/api/auth/login", summary="Sign in")
def login(body: LoginIn, request: Request, response: Response, conn=Depends(get_conn)):
    email = auth.normalize_email(body.email)
    _check_throttle(email)
    try:
        user = auth.authenticate(conn, email, body.password)
    except auth.AuthError as exc:
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


@app.get("/api/auth/me", summary="Signed-in user (null if nobody)")
def me(user=Depends(current_user)):
    return user


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
    return {"total": total, "items": items}


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


@app.post("/api/components/{comp_id}/equivalents", status_code=204, dependencies=[Depends(require_admin)],
          summary="Confirm that another component is equivalent")
def add_equivalent(comp_id: int, body: EquivalentIn, conn=Depends(get_conn)):
    try:
        alternates.confirm(conn, comp_id, body.other_id)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(status_code=204)


@app.delete("/api/components/{comp_id}/equivalents/{other_id}", status_code=204, dependencies=[Depends(require_admin)],
            summary="Remove a confirmed equivalence")
def remove_equivalent(comp_id: int, other_id: int, conn=Depends(get_conn)):
    alternates.unconfirm(conn, comp_id, other_id)
    return Response(status_code=204)


@app.get("/api/components/{comp_id}")
def get_component(comp_id: int, conn=Depends(get_conn)):
    comp = db.get(conn, comp_id)
    if not comp:
        raise HTTPException(404, "Component not found")
    return comp


@app.post("/api/components", status_code=201, dependencies=[Depends(require_admin)])
def create_component(body: ComponentIn, conn=Depends(get_conn)):
    try:
        cid = db.create(conn, body.model_dump())
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return db.get(conn, cid)


@app.put("/api/components/{comp_id}", dependencies=[Depends(require_admin)])
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


@app.post("/api/components/import", summary="Import / update the database from a CSV or XLSX file", dependencies=[Depends(require_admin)])
async def import_components(
    file: UploadFile = File(...),
    mapping: str = Form(default="", description=MAPPING_DOC),
    conn=Depends(get_conn),
):
    data = await _read_upload(file)
    overrides = _parse_mapping(mapping)
    try:
        return import_table(conn, data, file.filename or "", overrides)
    except TableError as exc:
        raise HTTPException(422, str(exc), headers=COLUMNS_ERROR) from exc


# ---------------------------------------------------------------- PDF datasheet

@app.post(
    "/api/datasheet/extract",
    summary="Extract a component's information from its PDF datasheet (proposal to review)",
    dependencies=[Depends(require_admin)],
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
    return info


@app.put("/api/manufacturers/{key}", dependencies=[Depends(require_admin)], summary="Edit a manufacturer's information")
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
    return exemptions.get(conn, code)


@app.put("/api/exemptions/{code}", dependencies=[Depends(require_admin)], summary="Edit an exemption's notes")
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
        return eccn.get(conn, code)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/eccn/{code}", dependencies=[Depends(require_admin)], summary="Edit an ECCN code's title and notes")
def update_eccn(code: str, body: EccnIn, conn=Depends(get_conn)):
    try:
        return eccn.update(conn, code, body.model_dump())
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/countries", summary="Recognised ISO country codes and their names")
def get_countries():
    return db.COUNTRIES


@app.get("/api/stats")
def get_stats(conn=Depends(get_conn)):
    return db.stats(conn) | {"admin_required": bool(ADMIN_TOKEN), "ai_available": datasheet_ai.available()}


# ---------------------------------------------------------------- Web interface

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
