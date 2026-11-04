"""Serveur HTTP : API REST + interface web.

Lancement :  uvicorn app.main:app --host 0.0.0.0 --port 8000
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Literal

from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import datasheet, datasheet_ai, db
from .analyzer import analyze
from .reports import database_csv, report_csv, report_xlsx
from .importer import import_table
from .tables import TableError, parse_bom

STATIC = Path(__file__).resolve().parent / "static"
MAX_UPLOAD = 10 * 1024 * 1024  # 10 Mo
MAX_DATASHEET = 30 * 1024 * 1024  # 30 Mo
ADMIN_TOKEN = os.environ.get("RRC_ADMIN_TOKEN", "")

app = FastAPI(
    title="Vérificateur RoHS / REACH",
    description="Analyse de conformité RoHS 2, RoHS 3 et REACH d'une nomenclature (BOM) à partir d'une base de composants.",
    version="1.0.0",
)


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


class ComponentIn(BaseModel):
    mpn: str
    manufacturer: str = ""
    description: str = ""
    rohs2: str = "inconnu"
    rohs3: str = "inconnu"
    reach: str = "inconnu"
    rohs_exemptions: str = ""
    svhc_list: str = ""
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
    format: Literal["json", "xlsx", "csv"] = Query(default="json"),
    conn=Depends(get_conn),
):
    data = await _read_upload(file)
    try:
        overrides = json.loads(mapping) if mapping else None
        lines, colmap = parse_bom(data, file.filename or "", overrides)
    except json.JSONDecodeError:
        raise HTTPException(400, "Paramètre mapping : JSON invalide")
    except TableError as exc:
        raise HTTPException(422, str(exc))
    report = analyze(conn, lines, colmap, file.filename or "")
    stem = Path(file.filename or "bom").stem
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
    return report


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
        raise HTTPException(422, str(exc))
    return db.get(conn, cid)


@app.put("/api/components/{comp_id}", dependencies=[Depends(require_admin)])
def update_component(comp_id: int, body: ComponentIn, conn=Depends(get_conn)):
    try:
        ok = db.update(conn, comp_id, body.model_dump())
    except db.ValidationError as exc:
        raise HTTPException(422, str(exc))
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
        raise HTTPException(422, str(exc))


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
        raise HTTPException(422, str(exc))

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


@app.get("/api/stats")
def get_stats(conn=Depends(get_conn)):
    return db.stats(conn) | {"admin_required": bool(ADMIN_TOKEN), "ai_available": datasheet_ai.available()}


# ---------------------------------------------------------------- Interface web

@app.get("/", include_in_schema=False)
def index():
    return FileResponse(STATIC / "index.html")


app.mount("/static", StaticFiles(directory=STATIC), name="static")
