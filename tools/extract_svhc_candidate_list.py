"""Convert ECHA's Candidate List export into data/svhc_candidate_list.json.

ECHA's site does not let scripts download the list, so the export is made by hand:
    1. open https://echa.europa.eu/candidate-list-table in a browser;
    2. click the export button above the table (Excel or CSV) and save the file;
    3. python tools/extract_svhc_candidate_list.py <exported file>

The header row is found automatically (columns "Substance name", "EC No.", "CAS No.", "Date of inclusion",
"Reason for inclusion"; the column order does not matter).
"""

from __future__ import annotations

import json
import re
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.svhc import CANDIDATE_LIST_URL, DATA_PATH  # noqa: E402
from app.tables import norm_header, read_rows  # noqa: E402

# Field -> accepted column names (normalised: lower case, no punctuation)
COLUMNS = {
    "name": ("substance name", "name"),
    "description": ("description",),
    "ec": ("ec no", "ec number", "ec"),
    "cas": ("cas no", "cas number", "cas"),
    "date": ("date of inclusion", "inclusion date", "date of inclusion in the candidate list"),
    "reason": ("reason for inclusion", "reason for inclusion in the candidate list", "reason"),
}
CAS_RE = re.compile(r"\b\d{2,7}-\d{2}-\d\b")
EC_RE = re.compile(r"\b\d{3}-\d{3}-\d\b")
ALIAS_RE = re.compile(r"\(([A-Z][A-Z0-9-]{1,11})\)")  # "Bis(2-ethylhexyl) phthalate (DEHP)" -> DEHP
DATE_FORMATS = ("%Y-%m-%d", "%Y-%m-%d %H:%M:%S", "%d/%m/%Y", "%d-%b-%Y", "%d %B %Y", "%d.%m.%Y", "%d-%m-%Y")


def find_header(rows: list[list[str]]) -> tuple[int, dict[str, int]]:
    for i, row in enumerate(rows[:30]):
        names = [norm_header(c) for c in row]
        cols = {}
        for field, accepted in COLUMNS.items():
            for j, n in enumerate(names):
                if n in accepted and j not in cols.values():
                    cols[field] = j
                    break
        if {"name", "cas", "date"} <= cols.keys():
            return i, cols
    raise SystemExit("Header row not found: expected the columns Substance name, CAS No. and Date of inclusion")


def parse_date(value: str) -> str:
    v = value.strip()
    for fmt in DATE_FORMATS:
        try:
            return datetime.strptime(v, fmt).date().isoformat()
        except ValueError:
            continue
    if re.fullmatch(r"\d{5}(\.0)?", v):  # Excel serial number
        return date.fromordinal(date(1899, 12, 30).toordinal() + int(float(v))).isoformat()
    return v


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:60]


def convert(path: Path) -> dict:
    rows = read_rows(path.read_bytes(), path.name)
    header, cols = find_header(rows)

    def cell(row: list[str], field: str) -> str:
        j = cols.get(field)
        return row[j].strip() if j is not None and j < len(row) else ""

    substances, ids = [], set()
    for row in rows[header + 1:]:
        name = re.sub(r"\s+", " ", cell(row, "name"))
        if not name:
            continue
        sid = base = slug(name) or f"substance-{len(substances) + 1}"
        n = 2
        while sid in ids:
            sid, n = f"{base}-{n}", n + 1
        ids.add(sid)
        substances.append({
            "id": sid,
            "name": name,
            "description": cell(row, "description"),
            "ec": EC_RE.findall(cell(row, "ec")),
            "cas": CAS_RE.findall(cell(row, "cas")),
            "aliases": sorted(set(ALIAS_RE.findall(name))),
            "date": parse_date(cell(row, "date")),
            "reason": re.sub(r"\s+", " ", cell(row, "reason")),
        })
    if not substances:
        raise SystemExit("No substance found below the header row")
    substances.sort(key=lambda s: (s["date"], s["name"].lower()), reverse=True)
    return {
        "source": "ECHA, Candidate List of substances of very high concern for Authorisation (REACH)",
        "url": CANDIDATE_LIST_URL,
        "version": max(s["date"] for s in substances),  # date of the latest update of the list
        "substances": substances,
    }


def main() -> None:
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    data = convert(Path(sys.argv[1]))
    DATA_PATH.write_text(json.dumps(data, ensure_ascii=False, indent=1) + "\n", encoding="utf-8")
    print(f"{len(data['substances'])} substances, latest inclusion {data['version']} -> {DATA_PATH.relative_to(ROOT)}")


if __name__ == "__main__":
    main()
