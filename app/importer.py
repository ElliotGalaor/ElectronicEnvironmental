"""Import en masse de la base de composants depuis un CSV / XLSX."""

from . import db
from .tables import TableError, apply_overrides, norm_header, read_rows, resolve_header_row, table_headers

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

# Champs proposés dans la fenêtre de correspondance des colonnes : (clé, libellé, obligatoire)
IMPORT_FIELDS = [
    ("mpn", "Référence fabricant", True),
    ("manufacturer", "Fabricant", False),
    ("description", "Description", False),
    ("rohs2", "RoHS 2", False),
    ("rohs3", "RoHS 3", False),
    ("reach", "REACH", False),
    ("rohs_exemptions", "Exemptions RoHS", False),
    ("svhc_list", "Substances SVHC", False),
    ("eccn", "ECCN", False),
    ("country_of_origin", "Pays d'origine", False),
    ("source", "Source", False),
    ("last_checked", "Vérifié le", False),
]


def _detect_header_row(rows: list[list[str]], max_scan: int = 25) -> int:
    """Première ligne contenant le plus d'en-têtes connus (la première ligne par défaut)."""
    known = {name for names in IMPORT_HEADERS.values() for name in names}
    best, best_score = 0, 0
    for i, row in enumerate(rows[:max_scan]):
        score = sum(norm_header(c) in known for c in row)
        if score > best_score:
            best, best_score = i, score
    return best


def detect_import_columns(rows: list[list[str]], mapping: dict | None = None) -> tuple[int, list[str], dict[str, int]]:
    """Ligne d'en-tête, noms des colonnes et correspondance {champ: index}, correspondance manuelle appliquée."""
    if not rows:
        raise TableError("Le fichier est vide")
    hr = resolve_header_row(rows, mapping, _detect_header_row)
    headers, labels = table_headers(rows, hr)
    normalized = [norm_header(h) for h in headers]
    colmap: dict[str, int] = {}
    for fld, names in IMPORT_HEADERS.items():
        for i, h in enumerate(normalized):
            if h in names and i not in colmap.values():
                colmap[fld] = i
                break
    apply_overrides(colmap, labels, mapping, IMPORT_HEADERS)
    return hr, labels, colmap


def import_table(conn, data: bytes, filename: str, mapping: dict | None = None) -> dict:
    """Import / mise à jour en masse (clé : référence + fabricant).

    mapping : correspondance manuelle des colonnes {champ: nom de colonne}, voir tables.apply_overrides.
    """
    rows = read_rows(data, filename)
    hr, _, colmap = detect_import_columns(rows, mapping)
    if "mpn" not in colmap:
        raise TableError("Colonne « référence fabricant » (mpn) introuvable : indiquez-la dans la correspondance des colonnes")
    created = updated = 0
    errors = []
    for n, row in enumerate(rows[hr + 1:], start=hr + 2):
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
