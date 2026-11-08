"""Serveur HTTP : API REST + interface web.

Lancement :  uvicorn app.main:app --host 0.0.0.0 --port 8000
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
from .importer import import_table
from .reports import database_csv, report_csv, report_xlsx
from .tables import TableError, apply_substitutions, modified_bom_csv, parse_bom

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 10 * 1024 * 1024  # 10 Mo
MAX_DATASHEET = 30 * 1024 * 1024  # 30 Mo
ADMIN_TOKEN = os.environ.get("RRC_ADMIN_TOKEN", "")

app = FastAPI(
    title="Vérificateur RoHS / REACH",
    description="Analyse de conformité RoHS 2, RoHS 3 et REACH d'une nomenclature (BOM) à partir d'une base de composants.",
    version="1.0.0",
)


@app.middleware("http")
async def no_stale_frontend(request, call_next):
    """Oblige le navigateur à revalider la page et ses scripts à chaque chargement.

    Sans cela, un navigateur peut garder un ancien app.js en cache avec un index.html récent,
    et les colonnes du tableau ne correspondent plus aux en-têtes.
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
    """Si RRC_ADMIN_TOKEN est défini, les modifications de la base exigent l'en-tête X-Admin-Token."""
    if ADMIN_TOKEN and x_admin_token != ADMIN_TOKEN:
        raise HTTPException(401, "Jeton administrateur requis (en-tête X-Admin-Token)")


def current_user(rrc_session: str | None = Cookie(default=None), conn=Depends(get_conn)) -> dict | None:
    """Utilisateur connecté (cookie de session), ou None."""
    return auth.user_from_token(conn, rrc_session)


def require_user(user=Depends(current_user)) -> dict:
    if not user:
        raise HTTPException(401, "Connexion requise")
    return user


class ComponentIn(BaseModel):
    mpn: str
    manufacturer: str = ""
    description: str = ""
    rohs2: str = "inconnu"
    rohs3: str = "inconnu"
    reach: str = "inconnu"
    rohs_exemptions: str = ""
    svhc_list: str = ""
    eccn: str = ""
    country_of_origin: str = ""
    source: str = ""
    last_checked: str = ""


async def _read_upload(file: UploadFile) -> bytes:
    data = await file.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "Fichier trop volumineux (max 10 Mo)")
    if not data:
        raise HTTPException(400, "Fichier vide")
    return data


# ---------------------------------------------------------------- Analyse BOM

@app.post("/api/analyze", summary="Analyser une BOM (CSV, TXT ou XLSX)")
async def analyze_bom(
    file: UploadFile = File(...),
    mapping: str = Form(default="", description='Surcharge des colonnes, JSON : {"mpn": "Part Number", ...}'),
    substitutions: str = Form(default="", description='Remplacements, JSON : {"<n° de ligne>": <id du composant>, ...}'),
    format: Literal["json", "xlsx", "csv", "bom"] = Query(default="json", description="bom : la nomenclature modifiée (remplacements appliqués), en CSV"),
    save: str = Form(default="", description="1 : enregistrer l'analyse dans l'historique de l'utilisateur connecté"),
    history_id: str = Form(default="", description="Entrée d'historique à mettre à jour (même BOM, réglages modifiés)"),
    conn=Depends(get_conn),
    user=Depends(current_user),
):
    data = await _read_upload(file)
    try:
        overrides = json.loads(mapping) if mapping else None
        subs = {int(k): int(v) for k, v in (json.loads(substitutions) if substitutions else {}).items()}
    except (ValueError, TypeError, AttributeError):
        raise HTTPException(400, "Paramètre mapping ou substitutions : JSON invalide") from None
    try:
        lines, colmap = parse_bom(data, file.filename or "", overrides)
    except TableError as exc:
        raise HTTPException(422, str(exc)) from exc
    try:
        replaced = apply_substitutions(conn, lines, subs)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    stem = Path(file.filename or "bom").stem
    if format == "bom":
        return Response(
            modified_bom_csv(data, file.filename or "", overrides, colmap, replaced),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="{stem}_modifiee.csv"'},
        )
    report = analyze(conn, lines, colmap, file.filename or "")
    if format == "xlsx":
        return Response(
            report_xlsx(report),
            media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
            headers={"Content-Disposition": f'attachment; filename="conformite_{stem}.xlsx"'},
        )
    if format == "csv":
        return Response(
            report_csv(report),
            media_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="conformite_{stem}.csv"'},
        )
    if user and (save == "1" or history_id):
        report["history_id"] = history.save(
            conn, user["id"], file.filename or "bom", data, mapping, substitutions, report,
            history_id=int(history_id) if history_id.isdigit() else None,
        )
    return report


# ---------------------------------------------------------------- Comptes utilisateurs

class RegisterIn(BaseModel):
    email: str
    password: str
    name: str = ""


class LoginIn(BaseModel):
    email: str
    password: str


# Limitation des essais de mot de passe : 5 échecs par adresse en 15 minutes
_LOGIN_WINDOW, _LOGIN_MAX = 15 * 60, 5
_login_failures: dict[str, list[float]] = defaultdict(list)


def _check_throttle(email: str) -> None:
    now = time.monotonic()
    recent = [t for t in _login_failures[email] if now - t < _LOGIN_WINDOW]
    _login_failures[email] = recent
    if len(recent) >= _LOGIN_MAX:
        raise HTTPException(429, "Trop de tentatives : réessayez dans quelques minutes")


def _open_session(conn, user: dict, request: Request, response: Response) -> dict:
    token = auth.create_session(conn, user["id"])
    response.set_cookie(
        auth.SESSION_COOKIE, token, max_age=auth.SESSION_DAYS * 86400, httponly=True, samesite="lax",
        secure=request.url.scheme == "https", path="/",
    )
    return user


@app.post("/api/auth/register", status_code=201, summary="Créer un compte (et se connecter)")
def register(body: RegisterIn, request: Request, response: Response, conn=Depends(get_conn)):
    try:
        user = auth.create_user(conn, body.email, body.password, body.name)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    except auth.AuthError as exc:
        raise HTTPException(409, str(exc)) from exc
    return _open_session(conn, user, request, response)


@app.post("/api/auth/login", summary="Se connecter")
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


@app.post("/api/auth/logout", status_code=204, summary="Se déconnecter")
def logout(response: Response, rrc_session: str | None = Cookie(default=None), conn=Depends(get_conn)):
    auth.delete_session(conn, rrc_session)
    response.delete_cookie(auth.SESSION_COOKIE, path="/")
    response.status_code = 204
    return response


@app.get("/api/auth/me", summary="Utilisateur connecté (null si personne)")
def me(user=Depends(current_user)):
    return user


# ---------------------------------------------------------------- Historique des BOM

@app.get("/api/history", summary="Dernières BOM analysées par l'utilisateur connecté")
def list_history(user=Depends(require_user), conn=Depends(get_conn)):
    return history.list_for(conn, user["id"])


@app.get("/api/history/{history_id}/file", summary="Fichier d'origine d'une BOM de l'historique")
def history_file(history_id: int, user=Depends(require_user), conn=Depends(get_conn)):
    found = history.get_file(conn, user["id"], history_id)
    if not found:
        raise HTTPException(404, "Analyse introuvable")
    filename, data = found
    safe = "".join(ch if ch.isalnum() or ch in "._-" else "_" for ch in filename) or "bom"
    return Response(data, media_type="application/octet-stream",
                    headers={"Content-Disposition": f'attachment; filename="{safe}"'})


@app.delete("/api/history/{history_id}", status_code=204, summary="Supprimer une BOM de l'historique")
def delete_history(history_id: int, user=Depends(require_user), conn=Depends(get_conn)):
    if not history.delete(conn, user["id"], history_id):
        raise HTTPException(404, "Analyse introuvable")
    return Response(status_code=204)


# ---------------------------------------------------------------- Base de composants

@app.get("/api/components", summary="Rechercher des composants")
def list_components(
    q: str = "",
    status: str = Query(default="", description="'probleme' pour n'afficher que les composants non conformes / inconnus"),
    limit: int = Query(default=50, ge=1, le=1000),
    offset: int = Query(default=0, ge=0),
    conn=Depends(get_conn),
):
    items, total = db.search(conn, q, limit, offset, status)
    return {"total": total, "items": items}


@app.get("/api/components/export", summary="Exporter toute la base en CSV")
def export_components(conn=Depends(get_conn)):
    return Response(
        database_csv(db.all_components(conn)),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": 'attachment; filename="base_composants.csv"'},
    )


@app.get("/api/alternatives", summary="Équivalents d'autres fabricants pour une référence quelconque (ex. ligne de BOM absente de la base)")
def alternatives_for(mpn: str = "", manufacturer: str = "", description: str = "", conn=Depends(get_conn)):
    return alternates.find(conn, mpn=mpn, manufacturer=manufacturer, description=description)


@app.get("/api/components/{comp_id}/alternatives", summary="Équivalents d'autres fabricants pour un composant de la base")
def component_alternatives(comp_id: int, conn=Depends(get_conn)):
    if not db.get(conn, comp_id):
        raise HTTPException(404, "Composant introuvable")
    return alternates.find(conn, component_id=comp_id)


class EquivalentIn(BaseModel):
    other_id: int


@app.post("/api/components/{comp_id}/equivalents", status_code=204, dependencies=[Depends(require_admin)],
          summary="Confirmer qu'un autre composant est équivalent")
def add_equivalent(comp_id: int, body: EquivalentIn, conn=Depends(get_conn)):
    try:
        alternates.confirm(conn, comp_id, body.other_id)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc
    return Response(status_code=204)


@app.delete("/api/components/{comp_id}/equivalents/{other_id}", status_code=204, dependencies=[Depends(require_admin)],
            summary="Retirer une équivalence confirmée")
def remove_equivalent(comp_id: int, other_id: int, conn=Depends(get_conn)):
    alternates.unconfirm(conn, comp_id, other_id)
    return Response(status_code=204)


@app.get("/api/components/{comp_id}")
def get_component(comp_id: int, conn=Depends(get_conn)):
    comp = db.get(conn, comp_id)
    if not comp:
        raise HTTPException(404, "Composant introuvable")
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
        raise HTTPException(404, "Composant introuvable")
    return db.get(conn, comp_id)


@app.delete("/api/components/{comp_id}", status_code=204, dependencies=[Depends(require_admin)])
def delete_component(comp_id: int, conn=Depends(get_conn)):
    if not db.delete(conn, comp_id):
        raise HTTPException(404, "Composant introuvable")
    return Response(status_code=204)


@app.post("/api/components/import", summary="Importer / mettre à jour la base depuis un CSV ou XLSX", dependencies=[Depends(require_admin)])
async def import_components(file: UploadFile = File(...), conn=Depends(get_conn)):
    data = await _read_upload(file)
    try:
        return import_table(conn, data, file.filename or "")
    except TableError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------- Datasheet PDF

@app.post(
    "/api/datasheet/extract",
    summary="Extraire les informations d'un composant depuis sa datasheet PDF (proposition à relire)",
    dependencies=[Depends(require_admin)],
)
def extract_datasheet(
    file: UploadFile = File(...),
    method: Literal["auto", "heuristique", "ia"] = Query(default="auto", description="auto : IA si une clé API est configurée, sinon heuristique"),
    conn=Depends(get_conn),
):
    data = file.file.read(MAX_DATASHEET + 1)
    if len(data) > MAX_DATASHEET:
        raise HTTPException(413, "PDF trop volumineux (max 30 Mo)")
    if not data:
        raise HTTPException(400, "Fichier vide")
    name = file.filename or "datasheet.pdf"

    try:
        heur = datasheet.extract(data, name)
    except datasheet.DatasheetError as exc:
        raise HTTPException(422, str(exc)) from exc

    use_ai = method == "ia" or (method == "auto" and datasheet_ai.available())
    if method == "ia" and not datasheet_ai.available():
        raise HTTPException(400, "Extraction IA indisponible : aucune clé ANTHROPIC_API_KEY configurée sur le serveur")
    result = heur
    if use_ai:
        try:
            result = datasheet_ai.extract(data, name)
            # Complète les champs vides par l'heuristique
            for key, val in heur["fields"].items():
                if not result["fields"].get(key) and val:
                    result["fields"][key] = val
            if not result["candidates"]["manufacturer"]:
                result["candidates"]["manufacturer"] = heur["candidates"]["manufacturer"]
            result["pages"] = heur["pages"]
        except datasheet_ai.AIExtractionError as exc:
            heur["warnings"].append(f"Extraction IA impossible ({exc}) : résultat heuristique affiché")

    # Le composant existe-t-il déjà ?
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


# ---------------------------------------------------------------- Fabricants
# Clé = nom normalisé du fabricant ; "_" désigne les composants sans fabricant renseigné.

class ManufacturerIn(BaseModel):
    name: str = ""
    website: str = ""
    country: str = ""
    notes: str = ""


def _mfr_key(key: str) -> str:
    return "" if key == "_" else key


@app.get("/api/manufacturers", summary="Liste des fabricants avec leurs statistiques")
def list_manufacturers(conn=Depends(get_conn)):
    return manufacturers.list_all(conn)


@app.get("/api/manufacturers/{key}", summary="Fiche d'un fabricant et ses composants")
def get_manufacturer(key: str, conn=Depends(get_conn)):
    info = manufacturers.get(conn, _mfr_key(key))
    if info is None:
        raise HTTPException(404, "Fabricant introuvable")
    return info


@app.put("/api/manufacturers/{key}", dependencies=[Depends(require_admin)], summary="Modifier les informations d'un fabricant")
def update_manufacturer(key: str, body: ManufacturerIn, conn=Depends(get_conn)):
    try:
        return manufacturers.update(conn, _mfr_key(key), body.model_dump())
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------- Exemptions RoHS

class ExemptionNotesIn(BaseModel):
    notes: str = ""


@app.get("/api/exemptions", summary="Exemptions RoHS utilisées par les composants de la base")
def list_exemptions(conn=Depends(get_conn)):
    return exemptions.list_all(conn)


@app.get("/api/exemptions/{code}", summary="Fiche d'une exemption RoHS : texte officiel, dates, composants")
def get_exemption(code: str, conn=Depends(get_conn)):
    return exemptions.get(conn, code)


@app.put("/api/exemptions/{code}", dependencies=[Depends(require_admin)], summary="Modifier les notes d'une exemption")
def update_exemption(code: str, body: ExemptionNotesIn, conn=Depends(get_conn)):
    try:
        return exemptions.update(conn, code, body.notes)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


# ---------------------------------------------------------------- Codes ECCN

class EccnIn(BaseModel):
    title: str = ""
    notes: str = ""


@app.get("/api/eccn", summary="Codes ECCN présents en base, avec leur explication")
def list_eccn(conn=Depends(get_conn)):
    return eccn.list_all(conn)


@app.get("/api/eccn/{code}", summary="Fiche d'un code ECCN : domaine d'application, définition officielle, composants")
def get_eccn(code: str, conn=Depends(get_conn)):
    try:
        return eccn.get(conn, code)
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/eccn/{code}", dependencies=[Depends(require_admin)], summary="Modifier l'intitulé et les notes d'un code ECCN")
def update_eccn(code: str, body: EccnIn, conn=Depends(get_conn)):
    try:
        return eccn.update(conn, code, body.model_dump())
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/countries", summary="Codes pays ISO reconnus et leur nom")
def get_countries():
    return db.COUNTRIES


@app.get("/api/stats")
def get_stats(conn=Depends(get_conn)):
    return db.stats(conn) | {"admin_required": bool(ADMIN_TOKEN), "ai_available": datasheet_ai.available()}


# ---------------------------------------------------------------- Interface web

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
