"""Exporting the analysis report as Excel or CSV, and exporting the database."""

from __future__ import annotations

import csv
import io
import re
from datetime import date

from .db import FIELDS, country_name, format_date, format_datetime

LABELS = {
    "compliant": "Compliant",
    "exemption": "Compliant (exemption)",
    "svhc": "SVHC > 0.1 %",
    "non_compliant": "NON-COMPLIANT",
    "needs_review": "Needs review",
    "dnp": "Not populated",
}

MATCH_LABELS = {
    "exact": "Exact",
    "mpn": "Part number only",
    "close": "Close",
    "mpn_other_manufacturer": "Other manufacturer",
    "partial": "Partial",
    "ambiguous": "Ambiguous",
    "not_found": "Not found",
    "-": "-",
}

COLUMNS = [
    ("Line", lambda l: l["line"]),
    ("Designators", lambda l: l["designator"]),
    ("Qty", lambda l: l["quantity"]),
    ("Internal reference", lambda l: l.get("internal_ref", "")),
    ("BOM part number", lambda l: l["bom_mpn"]),
    ("Replaces", lambda l: (l["original_mpn"] + (f" ({l['original_manufacturer']})" if l["original_manufacturer"] else "")) if l.get("original_mpn") else ""),
    ("BOM manufacturer", lambda l: l["bom_manufacturer"]),
    ("BOM description", lambda l: l["bom_description"]),
    ("Match", lambda l: MATCH_LABELS.get(l["match"], l["match"])),
    ("Database part number", lambda l: (l["component"] or {}).get("mpn", "")),
    ("Database manufacturer", lambda l: (l["component"] or {}).get("manufacturer", "")),
    ("RoHS 2", lambda l: _status(l, "rohs2")),
    ("RoHS 3", lambda l: _status(l, "rohs3")),
    ("REACH", lambda l: _status(l, "reach")),
    ("RoHS exemptions", lambda l: l["exemptions"]),
    ("SVHC", lambda l: l["svhc_list"]),
    ("ECCN", lambda l: (l["component"] or {}).get("eccn", "")),
    ("Country of origin", lambda l: _country((l["component"] or {}).get("country_of_origin", ""))),
    ("Source", lambda l: (l["component"] or {}).get("source", "")),
    ("Last checked", lambda l: format_date((l["component"] or {}).get("last_checked", ""))),
    ("Suggestions", lambda l: ", ".join(f"{s['mpn']} ({s['manufacturer']})" for s in l["suggestions"])),
    ("Notes", lambda l: " | ".join(l["notes"])),
]


# Column widths of the Excel report (16 by default)
WIDTHS = {"Line": 7, "Designators": 18, "Qty": 6, "Internal reference": 18, "BOM part number": 22, "Replaces": 22,
          "BOM manufacturer": 18, "BOM description": 30, "Match": 15, "Database part number": 22,
          "Database manufacturer": 18, "RoHS 2": 20, "RoHS 3": 20, "RoHS exemptions": 16, "SVHC": 30, "ECCN": 12,
          "Country of origin": 20, "Source": 18, "Last checked": 12, "Suggestions": 30, "Notes": 50}


def _columns(report: dict) -> list:
    """Report columns; the internal reference only when the BOM has one."""
    has_ref = any(line.get("internal_ref") for line in report["lines"])
    return [c for c in COLUMNS if has_ref or c[0] != "Internal reference"]


def _country(code: str) -> str:
    return f"{code} ({country_name(code)})" if code and country_name(code) != code else code


def _status(line: dict, norm: str) -> str:
    """Status for one regulation, with its origin when it does not come directly from the database."""
    text = LABELS.get(line[norm], line[norm])
    if line.get(f"{norm}_inferred"):
        text += " (inferred)"
    if norm in line.get("from_bom", []):
        text += " (from the BOM)"
    if norm in line.get("manual", []):
        text += " (classified by hand)"
    return text


FILLS = {
    "compliant": "C6EFCE",
    "exemption": "DDEBF7",
    "svhc": "FFE699",
    "non_compliant": "FFC7CE",
    "needs_review": "FCE4D6",
    "dnp": "EDEDED",
}


def report_csv(report: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    columns = _columns(report)
    w.writerow([c for c, _ in columns])
    for line in report["lines"]:
        w.writerow([f(line) for _, f in columns])
    return buf.getvalue().encode("utf-8-sig")


def report_xlsx(report: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Summary"
    s = report["summary"]
    bold = Font(bold=True)
    ws.append(["Environmental compliance report"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["BOM file", report.get("filename", "")])
    ws.append(["Analysed on", format_datetime(report["analyzed_at"])])
    ws.append(["Components in database", report["database_size"]])
    ws.append([])
    ws.append(["BOM lines", s["lines"]])
    ws.append(["Not populated (DNP)", s["dnp"]])
    ws.append(["Found in database", s["found"]])
    ws.append(["Uncertain matches", s["uncertain"]])
    ws.append(["Not found", s["not_found"]])
    if s.get("substituted"):
        ws.append(["Lines replaced by an equivalent", s["substituted"]])
    ex = s.get("export", {})
    if ex:
        ws.append([])
        ws.append(["Export / customs (identified components)"])
        ws.cell(ws.max_row, 1).font = bold
        ws.append(["Controlled ECCN (not EAR99)", ex["eccn_controlled"]])
        ws.append(["ECCN EAR99", ex["eccn_ear99"]])
        ws.append(["ECCN unknown", ex["eccn_unknown"]])
        ws.append(["Country of origin unknown", ex["country_unknown"]])
        if ex["countries"]:
            ws.append(["Countries of origin", ", ".join(f"{_country(c)}: {n}" for c, n in ex["countries"].items())])
    ws.append([])
    ws.append(["Regulation", "BOM verdict", "Compliant", "Exemption", "SVHC", "Non-compliant", "Needs review"])
    for c in ws[ws.max_row]:
        c.font = bold
    for norm, label in (("rohs2", "RoHS 2 (2011/65/EU)"), ("rohs3", "RoHS 3 (EU 2015/863)"), ("reach", "REACH (EC 1907/2006)")):
        d = s[norm]
        counts = d["counts"]
        ws.append([
            label,
            LABELS.get(d["verdict"], d["verdict"]),
            counts.get("compliant", 0),
            counts.get("exemption", 0),
            counts.get("svhc", 0),
            counts.get("non_compliant", 0),
            counts.get("needs_review", 0),
        ])
        ws.cell(ws.max_row, 2).fill = PatternFill("solid", fgColor=FILLS.get(d["verdict"], "FFFFFF"))
    ws.append([])
    ws.append(["Statuses taken from the server database. Check the manufacturers' declarations before any certification."])
    ws.column_dimensions["A"].width = 30
    for col in "BCDEFG":
        ws.column_dimensions[col].width = 16

    wd = wb.create_sheet("Detail")
    columns = _columns(report)
    wd.append([c for c, _ in columns])
    for c in wd[1]:
        c.font = bold
        c.fill = PatternFill("solid", fgColor="D9D9D9")
    norm_by_title = {"RoHS 2": "rohs2", "RoHS 3": "rohs3", "REACH": "reach"}
    status_cols = {i + 1: norm_by_title[name] for i, (name, _) in enumerate(columns) if name in norm_by_title}
    date_cols = [i + 1 for i, (name, _) in enumerate(columns) if name == "Last checked"]
    for line in report["lines"]:
        wd.append([f(line) for _, f in columns])
        for col in date_cols:  # real Excel dates (sortable), shown DD-MM-YYYY
            cell = wd.cell(wd.max_row, col)
            if isinstance(cell.value, str) and re.fullmatch(r"\d{2}-\d{2}-\d{4}", cell.value):
                d, m, y = (int(x) for x in cell.value.split("-"))
                cell.value, cell.number_format = date(y, m, d), "dd-mm-yyyy"
        for col, key in status_cols.items():
            wd.cell(wd.max_row, col).fill = PatternFill("solid", fgColor=FILLS.get(line[key], "FFFFFF"))
    for i, (name, _) in enumerate(columns, start=1):
        wd.column_dimensions[get_column_letter(i)].width = WIDTHS.get(name, 16)
    wd.freeze_panes = "A2"
    wd.auto_filter.ref = wd.dimensions
    for row in wd.iter_rows(min_row=2):
        for c in row:
            c.alignment = Alignment(vertical="top", wrap_text=True)

    out = io.BytesIO()
    wb.save(out)
    return out.getvalue()


def database_csv(components: list[dict]) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow(FIELDS)
    for c in components:
        w.writerow([format_date(c.get(f, "")) if f == "last_checked" else c.get(f, "") for f in FIELDS])
    return buf.getvalue().encode("utf-8-sig")
