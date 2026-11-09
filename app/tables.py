"""Reading tabular files (CSV, TSV, TXT, XLSX) and detecting the columns of a BOM."""

from __future__ import annotations

import csv
import io
import re
from dataclasses import dataclass, field

from .db import strip_accents


class TableError(ValueError):
    pass


def _decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    raise TableError("Unrecognised file encoding")


def read_rows(data: bytes, filename: str) -> list[list[str]]:
    """Return every row of the file as a list of strings."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover
            raise TableError("openpyxl is required to read Excel files") from exc
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise TableError(f"Unreadable Excel file: {exc}") from exc
        # Use the first non-empty sheet.
        for ws in wb.worksheets:
            rows = [["" if v is None else _cell_str(v) for v in row] for row in ws.iter_rows(values_only=True)]
            # Empty rows are kept so that row numbers match the file.
            while rows and not any(c.strip() for c in rows[-1]):
                rows.pop()
            if rows:
                return rows
        raise TableError("The Excel workbook is empty")
    if name.endswith(".xls"):
        raise TableError("The .xls format is not supported: save the file as .xlsx or .csv")

    text = _decode(data)
    sample = text[:20000]
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=";,\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        counts = {d: sample.count(d) for d in ";,\t|"}
        delimiter = max(counts, key=counts.get)
    rows = list(csv.reader(io.StringIO(text), delimiter=delimiter))
    rows = [[c.strip() for c in r] for r in rows]
    while rows and not any(rows[-1]):
        rows.pop()
    if not rows:
        raise TableError("The file is empty")
    return rows


def _cell_str(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def norm_header(h: str) -> str:
    h = strip_accents(h or "").lower()
    h = re.sub(r"[^a-z0-9]+", " ", h)
    return re.sub(r"\s+", " ", h).strip()


# Header synonyms (already normalised: lower case, no accents, no punctuation).
# French synonyms are kept: BOMs exported by French-language tools must still be recognised.
COLUMN_SYNONYMS: dict[str, list[str]] = {
    "mpn": [
        "mpn", "manufacturer part number", "manufacturer part no", "manufacturer part", "mfr part number",
        "mfr part", "mfr part no", "mfg part number", "mfg part", "mfr pn", "mfg pn", "part number", "part no",
        "pn", "p n", "reference fabricant", "ref fabricant", "ref fab", "reference constructeur",
        "ref constructeur", "numero de piece", "manufacturer pn", "mfr p n", "mfr ref",
        "manufacturer part number 1", "mpn 1", "reference composant", "ref composant",
    ],
    "manufacturer": [
        "manufacturer", "manufacturer name", "mfr", "mfr name", "mfg", "mfg name", "manufacturer 1", "fabricant",
        "constructeur", "marque", "vendor", "brand", "make", "nom fabricant",
    ],
    "description": ["description", "designation", "desc", "libelle", "value description", "comment", "commentaire"],
    "quantity": ["qty", "quantity", "quantite", "qte", "qt", "nb", "nombre", "quantity per pcb", "qty per board"],
    "designator": [
        "designator", "designators", "refdes", "ref des", "reference designator", "reference designators",
        "repere", "reperes", "references", "repere topologique", "reperes topologiques", "position", "positions",
    ],
    "value": ["value", "valeur", "val"],
    "dnp": ["dnp", "do not populate", "fitted", "populate", "monte", "non monte", "mounted", "assembly"],
    # Environmental statuses declared in the BOM (distributor or PLM export...)
    "rohs2": ["rohs2", "rohs 2", "rohs ii", "rohs 2011 65 ue", "rohs 2011 65 eu", "statut rohs 2", "rohs 2 status"],
    # "RoHS" alone: since July 2019, a stated RoHS compliance refers to Directive (EU) 2015/863 (RoHS 3)
    "rohs3": ["rohs3", "rohs 3", "rohs iii", "rohs 2015 863", "rohs 2015 863 ue", "rohs 2015 863 eu", "statut rohs 3",
              "rohs", "rohs status", "statut rohs", "rohs compliant", "rohs compliance", "conformite rohs"],
    "reach": ["reach", "reach status", "statut reach", "reach compliant", "reach compliance", "conformite reach", "reach svhc"],
}

_AMBIGUOUS_REFERENCE = {"reference", "ref", "references"}
_DESIGNATOR_VALUE_RE = re.compile(r"^\s*[A-Z]{1,4}\d+[A-Z]?(\s*[,;/ -]\s*[A-Z]{1,4}\d+[A-Z]?)*\s*$", re.I)


@dataclass
class ColumnMapping:
    header_row: int  # index of the header row (0 = first row, -1 = file without a header)
    headers: list[str]  # headers as written in the file
    columns: dict[str, int] = field(default_factory=dict)  # role -> column index
    labels: list[str] = field(default_factory=list)  # unique column names (see column_labels)

    def to_dict(self) -> dict:
        labels = self.labels or self.headers
        return {
            "header_row": self.header_row + 1,
            "headers": labels,
            "columns": {role: labels[idx] for role, idx in self.columns.items()},
        }


# ------------------------------------------------------------------ manual column mapping
# The mapping sent by the interface is a dictionary {field: column name}.
# "__none__" removes a wrongly detected field; the HEADER_ROW_KEY key sets the header row
# (row number in the file, 1 = first row; 0 = the file has no header row).

HEADER_ROW_KEY = "_header_row"
NO_COLUMN = "__none__"

# Fields offered in the column mapping dialog for a BOM: (key, label, required)
BOM_FIELDS = [
    ("mpn", "Manufacturer part number", True),
    ("manufacturer", "Manufacturer", False),
    ("designator", "Designators", False),
    ("quantity", "Quantity", False),
    ("description", "Description", False),
    ("value", "Value", False),
    ("dnp", "Do not populate (DNP)", False),
    ("rohs2", "RoHS 2", False),
    ("rohs3", "RoHS 3", False),
    ("reach", "REACH", False),
]


def column_labels(headers: list[str]) -> list[str]:
    """Unique name of each column: the header, "Column N" if empty, numbered if duplicated."""
    labels: list[str] = []
    for i, h in enumerate(headers):
        label = (h or "").strip() or f"Column {i + 1}"
        if label in labels:
            label = f"{label} (column {i + 1})"
        labels.append(label)
    return labels


def resolve_header_row(rows: list[list[str]], overrides: dict | None, detect) -> int:
    """Header row chosen by the user, otherwise detected. Returns an index (-1 = no header)."""
    raw = (overrides or {}).get(HEADER_ROW_KEY)
    if raw is None or raw == "":
        return detect(rows)
    try:
        n = int(raw)
    except (TypeError, ValueError) as exc:
        raise TableError(f"Invalid header row: {raw!r}") from exc
    if not 0 <= n <= len(rows):
        raise TableError(f"Header row {n} is outside the file (1 to {len(rows)}, or 0 if there is no header)")
    return n - 1


def table_headers(rows: list[list[str]], header_row: int) -> tuple[list[str], list[str]]:
    """Raw headers (padded to the table width) and unique column names."""
    width = max((len(r) for r in rows[: max(header_row, 0) + 200]), default=0)
    headers = [h.strip() for h in rows[header_row]] if header_row >= 0 else []
    headers += [""] * (width - len(headers))
    return headers, column_labels(headers)


def apply_overrides(columns: dict[str, int], labels: list[str], overrides: dict | None, allowed) -> None:
    """Apply the manual mapping {field: column name} on top of the detected columns."""
    allowed = set(allowed)
    for role, name in (overrides or {}).items():
        if role == HEADER_ROW_KEY or not name:
            continue
        if role not in allowed:
            raise TableError(f"Unknown field in the column mapping: {role!r}")
        if not isinstance(name, str):
            raise TableError(f"Column mapping: invalid column name for {role!r}")
        if name == NO_COLUMN:
            columns.pop(role, None)
            continue
        try:
            idx = labels.index(name)
        except ValueError as exc:
            raise TableError(f"Column {name!r} not found in the file") from exc
        for other, i in list(columns.items()):
            if i == idx and other != role:
                columns.pop(other)
        columns[role] = idx


def table_preview(rows: list[list[str]], header_row: int, labels: list[str], columns: dict[str, int],
                  fields: list[tuple[str, str, bool]], max_rows: int = 12) -> dict:
    """Preview of the file for the column mapping dialog."""
    width = len(labels)
    shown = max(max_rows, header_row + 6)
    return {
        "header_row": header_row + 1,
        "total_rows": len(rows),
        "headers": labels,
        "rows": [(r + [""] * (width - len(r)))[:width] for r in rows[:shown]],
        "columns": {role: labels[idx] for role, idx in columns.items()},
        "fields": [{"key": k, "label": label, "required": req} for k, label, req in fields],
        "missing": [k for k, _, req in fields if req and k not in columns],
    }


def _match_role(header: str) -> list[tuple[str, int]]:
    """Return the possible roles for a header, with a score (higher = better)."""
    h = norm_header(header)
    found = []
    for role, syns in COLUMN_SYNONYMS.items():
        if h in syns:
            found.append((role, 3))
        elif any(h.startswith(s + " ") or h.endswith(" " + s) for s in syns if len(s) > 3):
            found.append((role, 1))
    return found


def detect_header_row(rows: list[list[str]], max_scan: int = 25) -> int:
    best, best_score = 0, -1
    for i, row in enumerate(rows[:max_scan]):
        score = 0
        for cell in row:
            roles = _match_role(cell)
            if roles:
                score += max(s for _, s in roles)
            elif norm_header(cell) in _AMBIGUOUS_REFERENCE:
                score += 2
        if score > best_score:
            best, best_score = i, score
    return best


def detect_columns(rows: list[list[str]], overrides: dict[str, str] | None = None, require_mpn: bool = True) -> ColumnMapping:
    if not rows:
        raise TableError("The file is empty")
    hr = resolve_header_row(rows, overrides, detect_header_row)
    headers, labels = table_headers(rows, hr)
    data = rows[hr + 1 : hr + 51]
    mapping = ColumnMapping(header_row=hr, headers=headers, labels=labels)

    # Scores per role
    candidates: dict[str, list[tuple[int, int]]] = {}
    for idx, h in enumerate(headers):
        nh = norm_header(h)
        if nh in _AMBIGUOUS_REFERENCE:
            # "Reference": designator (R1, C2) or manufacturer part number? Look at the content.
            values = [r[idx] for r in data if idx < len(r) and r[idx]]
            looks_designator = values and sum(bool(_DESIGNATOR_VALUE_RE.match(v)) for v in values) / len(values) > 0.6
            candidates.setdefault("designator" if looks_designator else "mpn", []).append((2, idx))
            continue
        for role, score in _match_role(h):
            candidates.setdefault(role, []).append((score, idx))

    # Two passes: exactly recognised headers first, then partial matches
    # (otherwise "RoHS 2" could be taken by RoHS 3, which accepts "RoHS" at the start of a header).
    used: set[int] = set()
    for min_score in (2, 1):
        for role in ("mpn", "manufacturer", "designator", "quantity", "description", "value", "dnp", "rohs3", "rohs2", "reach"):
            if role in mapping.columns:
                continue
            for score, idx in sorted(candidates.get(role, []), key=lambda t: (-t[0], t[1])):
                if score >= min_score and idx not in used:
                    mapping.columns[role] = idx
                    used.add(idx)
                    break

    # Manual mapping: {role: column name}
    apply_overrides(mapping.columns, labels, overrides, COLUMN_SYNONYMS)

    if require_mpn and "mpn" not in mapping.columns:
        raise TableError(
            "Could not find the manufacturer part number (MPN) column. "
            "Select it in the column mapping. Columns in the file: " + ", ".join(labels)
        )
    return mapping


@dataclass
class BomLine:
    line: int  # row number in the file (1-based)
    mpn: str
    original_mpn: str = ""          # set when the line was replaced by an equivalent
    original_manufacturer: str = ""
    manufacturer: str = ""
    description: str = ""
    quantity: str = ""
    designator: str = ""
    value: str = ""
    dnp: bool = False
    # Statuses declared in the BOM (raw values, normalised by the analysis)
    rohs2: str = ""
    rohs3: str = ""
    reach: str = ""


_YES = {"yes", "oui", "y", "o", "1", "true", "x"}
_NO = {"no", "non", "n", "0", "false"}
_DNP_WORDS = {"dnp", "dnf", "nm", "not fitted", "non monte", "do not populate"}


def _is_dnp(header: str, value: str) -> bool:
    """Interpret the DNP column according to its meaning: "DNP = yes" or "Fitted = no"."""
    v = norm_header(value)
    if not v:
        return False
    if v in _DNP_WORDS:
        return True
    h = norm_header(header)
    if h in ("fitted", "populate", "monte", "mounted", "assembly"):
        return v in _NO
    return v in _YES


def parse_bom(data: bytes, filename: str, overrides: dict[str, str] | None = None) -> tuple[list[BomLine], ColumnMapping]:
    rows = read_rows(data, filename)
    mapping = detect_columns(rows, overrides)
    lines = bom_lines(rows, mapping)
    if not lines:
        raise TableError("No component line found below the header")
    return lines, mapping


def bom_lines(rows: list[list[str]], mapping: ColumnMapping) -> list[BomLine]:
    """Component lines read below the header, according to the column mapping."""
    c = mapping.columns

    def cell(row: list[str], role: str) -> str:
        i = c.get(role)
        return row[i].strip() if i is not None and i < len(row) else ""

    lines: list[BomLine] = []
    for offset, row in enumerate(rows[mapping.header_row + 1 :], start=mapping.header_row + 2):
        mpn = cell(row, "mpn")
        if not mpn and not cell(row, "designator") and not cell(row, "description"):
            continue  # empty or separator row
        dnp = False
        if "dnp" in c:
            dnp = _is_dnp(mapping.headers[c["dnp"]], cell(row, "dnp"))
        if norm_header(cell(row, "value")) in ("dnp", "dnf", "nm") or norm_header(mpn) in ("dnp", "dnf"):
            dnp = True
        lines.append(
            BomLine(
                line=offset,
                mpn=mpn,
                manufacturer=cell(row, "manufacturer"),
                description=cell(row, "description"),
                quantity=cell(row, "quantity"),
                designator=cell(row, "designator"),
                value=cell(row, "value"),
                dnp=dnp,
                rohs2=cell(row, "rohs2"),
                rohs3=cell(row, "rohs3"),
                reach=cell(row, "reach"),
            )
        )
    return lines


# ------------------------------------------------------------------ replacement by equivalents

def apply_substitutions(conn, lines: list[BomLine], subs: dict[int, int]) -> dict[int, dict]:
    """Replace the part number and manufacturer of the given lines with those of a database component.

    subs: {line number: component id}. Returns {line number: component} for the export.
    """
    from .db import ValidationError, get

    by_line = {l.line: l for l in lines}
    replaced = {}
    for line_no, comp_id in subs.items():
        line = by_line.get(line_no)
        comp = get(conn, comp_id)
        if line is None:
            raise ValidationError(f"Replacement: line {line_no} does not exist in the BOM")
        if comp is None:
            raise ValidationError(f"Replacement: component {comp_id} no longer exists in the database")
        line.original_mpn, line.original_manufacturer = line.mpn, line.manufacturer
        line.mpn, line.manufacturer = comp["mpn"], comp["manufacturer"]
        if comp.get("description"):
            line.description = comp["description"]
        replaced[line_no] = comp
    return replaced


def modified_bom_csv(data: bytes, filename: str, overrides, mapping: ColumnMapping, replaced: dict[int, dict]) -> bytes:
    """The original BOM (all its columns) with the replacements applied, as CSV (";" separator).

    A "Replaces" column gives the original part number of the modified lines.
    """
    rows = read_rows(data, filename)
    c = mapping.columns
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    if mapping.header_row < 0:  # file without a header: add one
        w.writerow((mapping.labels or mapping.headers) + ["Replaces"])
    for idx, row in enumerate(rows, start=1):
        row = list(row) + [""] * (len(mapping.headers) - len(row))
        if idx - 1 < mapping.header_row:
            w.writerow(row)
            continue
        if idx - 1 == mapping.header_row:
            w.writerow(row + ["Replaces"])
            continue
        comp = replaced.get(idx)
        note = ""
        if comp:
            old_mpn = row[c["mpn"]]
            old_mfr = row[c["manufacturer"]] if "manufacturer" in c else ""
            row[c["mpn"]] = comp["mpn"]
            if "manufacturer" in c:
                row[c["manufacturer"]] = comp["manufacturer"]
            note = f"{old_mpn} ({old_mfr})" if old_mfr else old_mpn
        w.writerow(row + [note])
    return out.getvalue().encode("utf-8-sig")
