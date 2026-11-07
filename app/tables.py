"""Lecture de fichiers tabulaires (CSV, TSV, TXT, XLSX) et détection des colonnes d'une BOM."""

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
    raise TableError("Encodage du fichier non reconnu")


def read_rows(data: bytes, filename: str) -> list[list[str]]:
    """Retourne toutes les lignes du fichier sous forme de listes de chaînes."""
    name = (filename or "").lower()
    if name.endswith((".xlsx", ".xlsm")):
        try:
            import openpyxl
        except ImportError as exc:  # pragma: no cover
            raise TableError("openpyxl est requis pour lire les fichiers Excel") from exc
        try:
            wb = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise TableError(f"Fichier Excel illisible : {exc}") from exc
        # On prend la première feuille non vide.
        for ws in wb.worksheets:
            rows = [["" if v is None else _cell_str(v) for v in row] for row in ws.iter_rows(values_only=True)]
            # On garde les lignes vides pour que les numéros de ligne correspondent au fichier.
            while rows and not any(c.strip() for c in rows[-1]):
                rows.pop()
            if rows:
                return rows
        raise TableError("Le classeur Excel est vide")
    if name.endswith(".xls"):
        raise TableError("Format .xls non supporté : enregistrez le fichier en .xlsx ou .csv")

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
        raise TableError("Le fichier est vide")
    return rows


def _cell_str(v) -> str:
    if isinstance(v, float) and v.is_integer():
        return str(int(v))
    return str(v).strip()


def norm_header(h: str) -> str:
    h = strip_accents(h or "").lower()
    h = re.sub(r"[^a-z0-9]+", " ", h)
    return re.sub(r"\s+", " ", h).strip()


# Synonymes d'en-têtes (déjà normalisés : minuscules, sans accents, sans ponctuation).
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
}

_AMBIGUOUS_REFERENCE = {"reference", "ref", "references"}
_DESIGNATOR_VALUE_RE = re.compile(r"^\s*[A-Z]{1,4}\d+[A-Z]?(\s*[,;/ -]\s*[A-Z]{1,4}\d+[A-Z]?)*\s*$", re.I)


@dataclass
class ColumnMapping:
    header_row: int
    headers: list[str]
    columns: dict[str, int] = field(default_factory=dict)  # rôle -> index de colonne

    def to_dict(self) -> dict:
        return {
            "header_row": self.header_row + 1,
            "headers": self.headers,
            "columns": {role: self.headers[idx] for role, idx in self.columns.items()},
        }


def _match_role(header: str) -> list[tuple[str, int]]:
    """Retourne les rôles possibles pour un en-tête, avec un score (plus haut = meilleur)."""
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


def detect_columns(rows: list[list[str]], overrides: dict[str, str] | None = None) -> ColumnMapping:
    if not rows:
        raise TableError("Le fichier est vide")
    hr = detect_header_row(rows)
    headers = [h.strip() for h in rows[hr]]
    data = rows[hr + 1 : hr + 51]
    mapping = ColumnMapping(header_row=hr, headers=headers)

    # Scores par rôle
    candidates: dict[str, list[tuple[int, int]]] = {}
    for idx, h in enumerate(headers):
        nh = norm_header(h)
        if nh in _AMBIGUOUS_REFERENCE:
            # "Reference" : repère (R1, C2) ou référence fabricant ? On regarde le contenu.
            values = [r[idx] for r in data if idx < len(r) and r[idx]]
            looks_designator = values and sum(bool(_DESIGNATOR_VALUE_RE.match(v)) for v in values) / len(values) > 0.6
            candidates.setdefault("designator" if looks_designator else "mpn", []).append((2, idx))
            continue
        for role, score in _match_role(h):
            candidates.setdefault(role, []).append((score, idx))

    used: set[int] = set()
    for role in ("mpn", "manufacturer", "designator", "quantity", "description", "value", "dnp"):
        for _score, idx in sorted(candidates.get(role, []), key=lambda t: (-t[0], t[1])):
            if idx not in used:
                mapping.columns[role] = idx
                used.add(idx)
                break

    # Surcharges manuelles : {rôle: nom d'en-tête}
    for role, header_name in (overrides or {}).items():
        if not header_name:
            continue
        if header_name == "__none__":
            mapping.columns.pop(role, None)
            continue
        try:
            idx = headers.index(header_name)
        except ValueError as exc:
            raise TableError(f"Colonne {header_name!r} introuvable dans le fichier") from exc
        for r, i in list(mapping.columns.items()):
            if i == idx and r != role:
                mapping.columns.pop(r)
        mapping.columns[role] = idx

    if "mpn" not in mapping.columns:
        raise TableError(
            "Impossible de trouver la colonne « référence fabricant » (MPN). "
            "Colonnes détectées : " + ", ".join(h for h in headers if h)
        )
    return mapping


@dataclass
class BomLine:
    line: int  # numéro de ligne dans le fichier (1-based)
    mpn: str
    original_mpn: str = ""          # renseignés quand la ligne a été remplacée par un équivalent
    original_manufacturer: str = ""
    manufacturer: str = ""
    description: str = ""
    quantity: str = ""
    designator: str = ""
    value: str = ""
    dnp: bool = False


_YES = {"yes", "oui", "y", "o", "1", "true", "x"}
_NO = {"no", "non", "n", "0", "false"}
_DNP_WORDS = {"dnp", "dnf", "nm", "not fitted", "non monte", "do not populate"}


def _is_dnp(header: str, value: str) -> bool:
    """Interprète la colonne DNP selon son sens : "DNP = oui" ou "Monté = non"."""
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
    c = mapping.columns

    def cell(row: list[str], role: str) -> str:
        i = c.get(role)
        return row[i].strip() if i is not None and i < len(row) else ""

    lines: list[BomLine] = []
    for offset, row in enumerate(rows[mapping.header_row + 1 :], start=mapping.header_row + 2):
        mpn = cell(row, "mpn")
        if not mpn and not cell(row, "designator") and not cell(row, "description"):
            continue  # ligne vide ou de séparation
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
            )
        )
    if not lines:
        raise TableError("Aucune ligne de composant trouvée sous l'en-tête")
    return lines, mapping


# ------------------------------------------------------------------ remplacements par des équivalents

def apply_substitutions(conn, lines: list[BomLine], subs: dict[int, int]) -> dict[int, dict]:
    """Remplace la référence et le fabricant des lignes indiquées par ceux d'un composant de la base.

    subs : {numéro de ligne: id du composant}. Retourne {numéro de ligne: composant} pour l'export.
    """
    from .db import ValidationError, get

    by_line = {l.line: l for l in lines}
    replaced = {}
    for line_no, comp_id in subs.items():
        line = by_line.get(line_no)
        comp = get(conn, comp_id)
        if line is None:
            raise ValidationError(f"Remplacement : la ligne {line_no} n'existe pas dans la BOM")
        if comp is None:
            raise ValidationError(f"Remplacement : le composant {comp_id} n'existe plus dans la base")
        line.original_mpn, line.original_manufacturer = line.mpn, line.manufacturer
        line.mpn, line.manufacturer = comp["mpn"], comp["manufacturer"]
        if comp.get("description"):
            line.description = comp["description"]
        replaced[line_no] = comp
    return replaced


def modified_bom_csv(data: bytes, filename: str, overrides, mapping: ColumnMapping, replaced: dict[int, dict]) -> bytes:
    """La BOM d'origine (toutes ses colonnes) avec les remplacements appliqués, en CSV (séparateur ;).

    Une colonne « Remplace » indique la référence d'origine des lignes modifiées.
    """
    rows = read_rows(data, filename)
    c = mapping.columns
    out = io.StringIO()
    w = csv.writer(out, delimiter=";")
    for idx, row in enumerate(rows, start=1):
        row = list(row) + [""] * (len(mapping.headers) - len(row))
        if idx - 1 < mapping.header_row:
            w.writerow(row)
            continue
        if idx - 1 == mapping.header_row:
            w.writerow(row + ["Remplace"])
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
