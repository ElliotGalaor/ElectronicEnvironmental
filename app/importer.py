"""Import en masse de la base de composants depuis un CSV / XLSX."""

from . import db
from .tables import TableError, norm_header, read_rows

IMPORT_HEADERS = {
    "mpn": ("mpn", "reference", "reference fabricant", "ref fabricant", "part number", "manufacturer part number"),
    "manufacturer": ("manufacturer", "fabricant", "constructeur", "mfr"),
    "description": ("description", "designation"),
    "rohs2": ("rohs2", "rohs 2", "rohs ii", "rohs 2011 65 ue"),
    "rohs3": ("rohs3", "rohs 3", "rohs iii", "rohs 2015 863", "rohs 2015 863 ue"),
    "reach": ("reach",),
    "rohs_exemptions": ("rohs exemptions", "exemptions", "exemption", "exemptions rohs"),
    "svhc_list": ("svhc list", "svhc", "substances svhc", "liste svhc"),
    "eccn": ("eccn", "eccn us", "us eccn", "export control classification number", "classification export", "eu eccn"),
    "country_of_origin": ("country of origin", "country_of_origin", "coo", "origin", "origine", "pays d origine", "pays origine", "pays de fabrication", "made in"),
    "source": ("source", "document", "declaration"),
    "last_checked": ("last checked", "verifie le", "date verification", "date"),
}


def import_table(conn, data: bytes, filename: str) -> dict:
    """Import / mise à jour en masse (clé : référence + fabricant)."""
    rows = read_rows(data, filename)
    headers = [norm_header(h) for h in rows[0]]
    colmap = {}
    for field, names in IMPORT_HEADERS.items():
        for i, h in enumerate(headers):
            if h in names and i not in colmap.values():
                colmap[field] = i
                break
    if "mpn" not in colmap:
        raise TableError("Colonne 'mpn' (référence fabricant) introuvable dans la première ligne")
    created = updated = 0
    errors = []
    for n, row in enumerate(rows[1:], start=2):
        rec = {f: (row[i] if i < len(row) else "") for f, i in colmap.items()}
        if not rec.get("mpn", "").strip():
            continue
        try:
            _, was_created = db.upsert(conn, rec)
            created += was_created
            updated += not was_created
        except db.ValidationError as exc:
            errors.append(f"Ligne {n} : {exc}")
    return {"created": created, "updated": updated, "errors": errors[:100], "error_count": len(errors)}
