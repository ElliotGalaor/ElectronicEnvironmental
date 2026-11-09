"""Bulk import of the component database from a CSV / XLSX file."""

from . import db
from .tables import TableError, apply_overrides, norm_header, read_rows, resolve_header_row, table_headers

# Recognised headers (normalised), in English and French
IMPORT_HEADERS = {
    "mpn": ("mpn", "reference", "reference fabricant", "ref fabricant", "part number", "manufacturer part number"),
    "manufacturer": ("manufacturer", "fabricant", "constructeur", "mfr"),
    "description": ("description", "designation"),
    "rohs2": ("rohs2", "rohs 2", "rohs ii", "rohs 2011 65 ue", "rohs 2011 65 eu"),
    "rohs3": ("rohs3", "rohs 3", "rohs iii", "rohs 2015 863", "rohs 2015 863 ue", "rohs 2015 863 eu"),
    "reach": ("reach",),
    "rohs_exemptions": ("rohs exemptions", "exemptions", "exemption", "exemptions rohs"),
    "svhc_list": ("svhc list", "svhc", "svhc substances", "substances svhc", "liste svhc"),
    "eccn": ("eccn", "eccn us", "us eccn", "export control classification number", "export classification",
             "classification export", "eu eccn"),
    "country_of_origin": ("country of origin", "country_of_origin", "coo", "origin", "origine", "pays d origine",
                          "pays origine", "pays de fabrication", "made in"),
    "source": ("source", "document", "declaration"),
    "last_checked": ("last checked", "checked on", "verifie le", "date verification", "date"),
}

# Fields offered in the column mapping dialog: (key, label, required)
IMPORT_FIELDS = [
    ("mpn", "Manufacturer part number", True),
    ("manufacturer", "Manufacturer", False),
    ("description", "Description", False),
    ("rohs2", "RoHS 2", False),
    ("rohs3", "RoHS 3", False),
    ("reach", "REACH", False),
    ("rohs_exemptions", "RoHS exemptions", False),
    ("svhc_list", "SVHC substances", False),
    ("eccn", "ECCN", False),
    ("country_of_origin", "Country of origin", False),
    ("source", "Source", False),
    ("last_checked", "Last checked", False),
]


def _detect_header_row(rows: list[list[str]], max_scan: int = 25) -> int:
    """First row containing the most known headers (the first row by default)."""
    known = {name for names in IMPORT_HEADERS.values() for name in names}
    best, best_score = 0, 0
    for i, row in enumerate(rows[:max_scan]):
        score = sum(norm_header(c) in known for c in row)
        if score > best_score:
            best, best_score = i, score
    return best


def detect_import_columns(rows: list[list[str]], mapping: dict | None = None) -> tuple[int, list[str], dict[str, int]]:
    """Header row, column names and mapping {field: index}, with the manual mapping applied."""
    if not rows:
        raise TableError("The file is empty")
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


def _records(rows: list[list[str]], header_row: int, colmap: dict[str, int]):
    """File rows converted to records {field: value} (only the fields mapped to a column)."""
    for n, row in enumerate(rows[header_row + 1:], start=header_row + 2):
        rec = {f: (row[i].strip() if i < len(row) else "") for f, i in colmap.items()}
        if rec.get("mpn"):
            yield n, rec


def plan_row(conn, rec: dict) -> dict:
    """Simulate the import of one row, without writing anything.

    action: create (new component), update (fields changed), unchanged, error.
    For an existing component, only fields mapped to a column and filled in are changed:
    an ignored column or an empty cell must not erase what is already in the database.
    """
    try:
        new = db.prepare(rec)
    except db.ValidationError as exc:
        return {"action": "error", "error": str(exc), "values": rec, "changes": {}}
    values = {f: new[f] for f in rec}
    row = conn.execute(
        "SELECT * FROM components WHERE mpn_norm = ? AND manufacturer_norm = ?",
        (new["mpn_norm"], new["manufacturer_norm"]),
    ).fetchone()
    if row is None:
        return {"action": "create", "values": values, "changes": {}}
    old = {f: row[f] for f in db.FIELDS}
    provided = {f for f, v in rec.items() if v}
    values = {f: values[f] if f in provided else old[f] for f in values}
    changes = {f: [old[f], values[f]] for f in provided if old[f] != values[f]}
    return {"action": "update" if changes else "unchanged", "id": row["id"], "old": old, "values": values,
            "changes": changes, "provided": sorted(provided)}


def plan_table(conn, rows: list[list[str]], header_row: int, colmap: dict[str, int], sample: int = 10) -> dict:
    """Import summary (all rows) and detail of the first rows, for the preview."""
    counts = {"create": 0, "update": 0, "unchanged": 0, "error": 0}
    detail = []
    for n, rec in _records(rows, header_row, colmap):
        plan = plan_row(conn, rec)
        counts[plan["action"]] += 1
        if len(detail) < sample:
            detail.append({"line": n, **{k: v for k, v in plan.items() if k not in ("id", "old", "provided")}})
    return {"counts": counts, "lines": detail}


def import_table(conn, data: bytes, filename: str, mapping: dict | None = None) -> dict:
    """Bulk import / update (key: part number + manufacturer).

    mapping: manual column mapping {field: column name}, see tables.apply_overrides.
    For components already in the database, fields without a column and empty cells are not changed.
    """
    rows = read_rows(data, filename)
    hr, _, colmap = detect_import_columns(rows, mapping)
    if "mpn" not in colmap:
        raise TableError("Manufacturer part number (mpn) column not found: select it in the column mapping")
    counts = {"create": 0, "update": 0, "unchanged": 0}
    errors = []
    for n, rec in _records(rows, hr, colmap):
        plan = plan_row(conn, rec)
        try:
            if plan["action"] == "error":
                raise db.ValidationError(plan["error"])
            if plan["action"] == "create":
                db.create(conn, rec)
            elif plan["action"] == "update":
                db.update(conn, plan["id"], {**plan["old"], **{f: rec[f] for f in plan["provided"]}})
            counts[plan["action"]] += 1
        except db.ValidationError as exc:
            errors.append(f"Row {n}: {exc}")
    return {
        "created": counts["create"], "updated": counts["update"], "unchanged": counts["unchanged"],
        "errors": errors[:100], "error_count": len(errors),
    }
