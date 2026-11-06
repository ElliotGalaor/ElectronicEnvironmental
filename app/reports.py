"""Export du rapport d'analyse en Excel ou CSV, et export/import de la base."""

from __future__ import annotations

import csv
import io

from .db import FIELDS, country_name

LABELS = {
    "conforme": "Conforme",
    "exemption": "Conforme (exemption)",
    "svhc": "SVHC > 0,1 %",
    "non_conforme": "NON CONFORME",
    "a_verifier": "À vérifier",
    "non_monte": "Non monté",
}

MATCH_LABELS = {
    "exact": "Exacte",
    "mpn": "Référence seule",
    "approchant": "Approchante",
    "mpn_autre_fabricant": "Autre fabricant",
    "partiel": "Partielle",
    "ambigu": "Ambiguë",
    "introuvable": "Introuvable",
    "-": "-",
}

COLUMNS = [
    ("Ligne", lambda l: l["line"]),
    ("Repères", lambda l: l["designator"]),
    ("Qté", lambda l: l["quantity"]),
    ("Réf. BOM", lambda l: l["bom_mpn"]),
    ("Remplace", lambda l: (l["original_mpn"] + (f" ({l['original_manufacturer']})" if l["original_manufacturer"] else "")) if l.get("original_mpn") else ""),
    ("Fabricant BOM", lambda l: l["bom_manufacturer"]),
    ("Description BOM", lambda l: l["bom_description"]),
    ("Correspondance", lambda l: MATCH_LABELS.get(l["match"], l["match"])),
    ("Réf. base", lambda l: (l["component"] or {}).get("mpn", "")),
    ("Fabricant base", lambda l: (l["component"] or {}).get("manufacturer", "")),
    ("RoHS 2", lambda l: LABELS.get(l["rohs2"], l["rohs2"]) + (" (déduit)" if l["rohs2_inferred"] else "")),
    ("RoHS 3", lambda l: LABELS.get(l["rohs3"], l["rohs3"]) + (" (déduit)" if l["rohs3_inferred"] else "")),
    ("REACH", lambda l: LABELS.get(l["reach"], l["reach"])),
    ("Exemptions RoHS", lambda l: l["exemptions"]),
    ("SVHC", lambda l: l["svhc_list"]),
    ("ECCN", lambda l: (l["component"] or {}).get("eccn", "")),
    ("Pays d'origine", lambda l: _country((l["component"] or {}).get("country_of_origin", ""))),
    ("Source", lambda l: (l["component"] or {}).get("source", "")),
    ("Vérifié le", lambda l: (l["component"] or {}).get("last_checked", "")),
    ("Suggestions", lambda l: ", ".join(f"{s['mpn']} ({s['manufacturer']})" for s in l["suggestions"])),
    ("Remarques", lambda l: " | ".join(l["notes"])),
]

def _country(code: str) -> str:
    return f"{code} ({country_name(code)})" if code and country_name(code) != code else code


FILLS = {
    "conforme": "C6EFCE",
    "exemption": "DDEBF7",
    "svhc": "FFE699",
    "non_conforme": "FFC7CE",
    "a_verifier": "FCE4D6",
    "non_monte": "EDEDED",
}


def report_csv(report: dict) -> bytes:
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=";")
    w.writerow([c for c, _ in COLUMNS])
    for line in report["lines"]:
        w.writerow([f(line) for _, f in COLUMNS])
    return buf.getvalue().encode("utf-8-sig")


def report_xlsx(report: dict) -> bytes:
    from openpyxl import Workbook
    from openpyxl.styles import Alignment, Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    ws = wb.active
    ws.title = "Synthèse"
    s = report["summary"]
    bold = Font(bold=True)
    ws.append(["Rapport de conformité environnementale"])
    ws["A1"].font = Font(bold=True, size=14)
    ws.append(["Fichier BOM", report.get("filename", "")])
    ws.append(["Analysé le", report["analyzed_at"]])
    ws.append(["Composants en base", report["database_size"]])
    ws.append([])
    ws.append(["Lignes de BOM", s["lines"]])
    ws.append(["Non montées (DNP)", s["dnp"]])
    ws.append(["Trouvées en base", s["found"]])
    ws.append(["Correspondances incertaines", s["uncertain"]])
    ws.append(["Introuvables", s["not_found"]])
    if s.get("substituted"):
        ws.append(["Lignes remplacées par un équivalent", s["substituted"]])
    ex = s.get("export", {})
    if ex:
        ws.append([])
        ws.append(["Export / douane (composants identifiés)"])
        ws.cell(ws.max_row, 1).font = bold
        ws.append(["ECCN contrôlé (hors EAR99)", ex["eccn_controlled"]])
        ws.append(["ECCN EAR99", ex["eccn_ear99"]])
        ws.append(["ECCN inconnu", ex["eccn_unknown"]])
        ws.append(["Pays d'origine inconnu", ex["country_unknown"]])
        if ex["countries"]:
            ws.append(["Pays d'origine", ", ".join(f"{_country(c)} : {n}" for c, n in ex["countries"].items())])
    ws.append([])
    ws.append(["Norme", "Verdict BOM", "Conformes", "Exemption", "SVHC", "Non conformes", "À vérifier"])
    for c in ws[ws.max_row]:
        c.font = bold
    for norm, label in (("rohs2", "RoHS 2 (2011/65/UE)"), ("rohs3", "RoHS 3 (UE 2015/863)"), ("reach", "REACH (CE 1907/2006)")):
        d = s[norm]
        counts = d["counts"]
        ws.append([
            label,
            LABELS.get(d["verdict"], d["verdict"]),
            counts.get("conforme", 0),
            counts.get("exemption", 0),
            counts.get("svhc", 0),
            counts.get("non_conforme", 0),
            counts.get("a_verifier", 0),
        ])
        ws.cell(ws.max_row, 2).fill = PatternFill("solid", fgColor=FILLS.get(d["verdict"], "FFFFFF"))
    ws.append([])
    ws.append(["Statuts issus de la base de données serveur. Vérifier les déclarations fabricants avant toute attestation."])
    ws.column_dimensions["A"].width = 30
    for col in "BCDEFG":
        ws.column_dimensions[col].width = 16

    wd = wb.create_sheet("Détail")
    wd.append([c for c, _ in COLUMNS])
    for c in wd[1]:
        c.font = bold
        c.fill = PatternFill("solid", fgColor="D9D9D9")
    norm_by_title = {"RoHS 2": "rohs2", "RoHS 3": "rohs3", "REACH": "reach"}
    status_cols = {i + 1: norm_by_title[name] for i, (name, _) in enumerate(COLUMNS) if name in norm_by_title}
    for line in report["lines"]:
        wd.append([f(line) for _, f in COLUMNS])
        for col, key in status_cols.items():
            wd.cell(wd.max_row, col).fill = PatternFill("solid", fgColor=FILLS.get(line[key], "FFFFFF"))
    widths = [7, 18, 6, 22, 22, 18, 30, 15, 22, 18, 20, 20, 16, 16, 30, 12, 20, 18, 12, 30, 50]
    for i, w in enumerate(widths, start=1):
        wd.column_dimensions[get_column_letter(i)].width = w
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
        w.writerow([c.get(f, "") for f in FIELDS])
    return buf.getvalue().encode("utf-8-sig")
