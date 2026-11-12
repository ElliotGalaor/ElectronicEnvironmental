"""ECCN codes: explanation, official definition and affected components.

An ECCN (Export Control Classification Number) from the US Commerce Control List
(15 CFR part 774, Supplement No. 1) reads as follows, for example 5A992.c:
  - 5     : category (telecommunications and information security);
  - A     : product group (systems, equipment and components);
  - 992   : entry number; its hundreds digit gives the reason for control (9xx: US
            unilateral controls, mainly anti-terrorism);
  - .c    : sub-paragraph of the entry.
EAR99 designates an item subject to the EAR but not on the list (15 CFR 734.3(c)).

The official entry headings come from the eCFR text (data/ccl_headings.json, public domain).
The explanations per category / group / reason for control are a summary.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone
from functools import lru_cache
from pathlib import Path

from . import db

CCL_URL = "https://www.ecfr.gov/current/title-15/subtitle-B/chapter-VII/subchapter-C/part-774/appendix-Supplement%20No.%201%20to%20Part%20774"
EAR99_URL = "https://www.ecfr.gov/current/title-15/subtitle-B/chapter-VII/subchapter-C/part-734/section-734.3"
COUNTRY_CHART_URL = "https://www.ecfr.gov/current/title-15/subtitle-B/chapter-VII/subchapter-C/part-738"

CATEGORIES = {
    "0": ("Nuclear materials, facilities, equipment and miscellaneous items",
          "Nuclear materials and equipment, plus miscellaneous items (firearms, “600 series” military items...)."),
    "1": ("Special materials, chemicals, microorganisms and toxins",
          "Composite materials, chemicals, biological agents and toxins, and related equipment."),
    "2": ("Materials processing",
          "Machine tools, robots, manufacturing and materials processing equipment."),
    "3": ("Electronics",
          "Electronic components and equipment: integrated circuits, discrete components, semiconductor test and manufacturing equipment."),
    "4": ("Computers",
          "Computers, electronic assemblies and related equipment."),
    "5": ("Telecommunications and information security",
          "Part 1: telecommunications equipment. Part 2: information security (cryptography)."),
    "6": ("Sensors and lasers",
          "Acoustic and optical sensors, cameras, lasers, magnetometers, radars."),
    "7": ("Navigation and avionics",
          "Navigation systems, gyroscopes, accelerometers, avionics."),
    "8": ("Marine", "Vessels, submersibles, marine systems and equipment."),
    "9": ("Aerospace and propulsion", "Engines, propulsion systems, spacecraft, aircraft and their components."),
}

GROUPS = {
    "A": ("Systems, equipment and components", "The hardware itself: equipment, modules, components."),
    "B": ("Test, inspection and production equipment", "Hardware used to test, inspect or manufacture."),
    "C": ("Materials", "Materials and substances."),
    "D": ("Software", "Software related to the items of the category."),
    "E": ("Technology", "Technology (know-how, technical data) for development, production or use."),
}


def control_regime(number: int) -> tuple[str, str]:
    """Reason for control given by the entry number (last 3 digits)."""
    if number < 100:
        return ("National security (Wassenaar Arrangement)",
                "Dual-use items controlled under multilateral agreements: the strictest control, a licence is often required outside allied countries.")
    if number < 200:
        return ("Missile technology (MTCR)", "Controlled under the Missile Technology Control Regime.")
    if number < 300:
        return ("Nuclear non-proliferation (NSG)", "Controlled under the Nuclear Suppliers Group.")
    if number < 400:
        return ("Chemical and biological weapons (Australia Group)", "Controlled under the Australia Group.")
    if 500 <= number < 600:
        return ("“500 series” (spacecraft)", "Space items moved from the US Munitions List (USML).")
    if 600 <= number < 700:
        return ("“600 series” (military items)", "Military items moved from the US Munitions List (USML): strict control.")
    if number >= 900:
        return ("US unilateral controls (9xx)",
                "Mainly anti-terrorism (AT): a licence is only required for a small number of embargoed countries or targeted end users. Light control.")
    return ("Other", "Reason for control to be checked in the official definition.")


@lru_cache(maxsize=1)
def _headings() -> tuple[dict, str]:
    path = Path(__file__).resolve().parent.parent / "data" / "ccl_headings.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("headings", {}), data.get("as_of", "")
    except (OSError, ValueError):
        return {}, ""


_CODE_RE = re.compile(r"^(\d)([A-E])(\d{3})((?:\.[a-z0-9]+)*)$")


def describe(code: str) -> dict:
    """Explanation of an ECCN code (without database access)."""
    headings, as_of = _headings()
    if code == "EAR99":
        return {
            "code": "EAR99",
            "controlled": False,
            "summary": "Item subject to the EAR but not on the Commerce Control List.",
            "explanation": "EAR99 covers the vast majority of everyday commercial products (passive components, standard circuits...).",
            "control_scope": ("No licence is generally required, except for export to an embargoed country, "
                              "to a restricted party or for a prohibited end use."),
            "parts": [],
            "official_heading": "",
            "links": [
                {"label": "Official definition: 15 CFR 734.3(c) (eCFR)", "url": EAR99_URL},
            ],
            "as_of": as_of,
        }
    m = _CODE_RE.match(code)
    if not m:
        return {"code": code, "controlled": True, "summary": "Unrecognised code", "explanation": "", "parts": [],
                "official_heading": "", "links": [{"label": "Commerce Control List (eCFR)", "url": CCL_URL}], "as_of": as_of}
    cat, grp, num, sub = m.group(1), m.group(2), m.group(3), m.group(4)
    entry = f"{cat}{grp}{num}"
    cat_name, cat_desc = CATEGORIES[cat]
    grp_name, grp_desc = GROUPS[grp]
    reg_name, reg_desc = control_regime(int(num))
    heading = headings.get(entry, "")
    parts = [
        {"code": cat, "label": "Category", "name": cat_name, "description": cat_desc},
        {"code": grp, "label": "Product group", "name": grp_name, "description": grp_desc},
        {"code": num, "label": "Reason for control", "name": reg_name, "description": reg_desc},
    ]
    if sub:
        parts.append({"code": sub, "label": "Sub-paragraph", "name": f"paragraph {sub.lstrip('.')} of entry {entry}",
                      "description": "Identifies the item within the entry: see the “Items” list of the entry in the official definition."})
    return {
        "code": code,
        "entry": entry,
        "controlled": True,
        "summary": f"{cat_name} — {grp_name.lower()}",
        "explanation": (f"Entry {entry} of category {cat} ({cat_name.lower()}), group {grp} "
                        f"({grp_name.lower()}), controlled for: {reg_name[0].lower() + reg_name[1:]}."
                        + (f" Sub-paragraph {sub.lstrip('.')} designates a specific item of this entry." if sub else "")),
        "control_scope": reg_desc,
        "parts": parts,
        "official_heading": heading,
        "links": [
            {"label": f"Official definition of entry {entry}: Commerce Control List, 15 CFR part 774 (eCFR)", "url": CCL_URL,
             "hint": f"On the page, search for “{entry}” (Ctrl+F)."},
            {"label": "Countries requiring a licence by reason for control: Commerce Country Chart, 15 CFR part 738 (eCFR)", "url": COUNTRY_CHART_URL},
        ],
        "as_of": as_of,
    }


# ------------------------------------------------------------------ database

def _stored(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM eccn_codes WHERE code = ?", (code,)).fetchone()


def list_all(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT eccn, manufacturer, manufacturer_norm FROM components WHERE eccn != ''").fetchall()
    counts = Counter(r["eccn"] for r in rows)
    by_code: dict[str, list] = {}
    for r in rows:
        by_code.setdefault(r["eccn"], []).append(r)
    stored = {r["code"]: r for r in conn.execute("SELECT * FROM eccn_codes")}
    unknown = conn.execute("SELECT COUNT(*) FROM components WHERE eccn = ''").fetchone()[0]
    out = []
    for code in set(counts) | set(stored):
        d = describe(code)
        out.append({
            "code": code,
            "controlled": d["controlled"],
            "summary": d["summary"],
            "official_heading": d["official_heading"],
            "title": stored[code]["title"] if code in stored else "",
            "components": counts.get(code, 0),
            "manufacturers": sorted({r["manufacturer"] for r in by_code.get(code, [])}),
            "manufacturer_links": sorted(db.manufacturer_links(by_code.get(code, [])), key=lambda m: m["name"].lower()),
        })
    out.sort(key=lambda e: (not e["controlled"], e["code"]))
    return {"codes": out, "unknown_components": unknown}


def get(conn: sqlite3.Connection, code: str) -> dict:
    code = db.normalize_eccn(code)
    d = describe(code)
    rows = conn.execute("SELECT * FROM components WHERE eccn = ? ORDER BY manufacturer, mpn", (code,)).fetchall()
    # Variants: 5A992 groups 5A992.c, etc.
    related = []
    if d.get("entry"):
        related = [r[0] for r in conn.execute(
            "SELECT DISTINCT eccn FROM components WHERE eccn != ? AND (eccn = ? OR eccn LIKE ?) ORDER BY eccn",
            (code, d["entry"], d["entry"] + ".%"))]
    stored = _stored(conn, code)
    return d | {
        "title": stored["title"] if stored else "",
        "notes": stored["notes"] if stored else "",
        "components": [db.row_to_dict(r) for r in rows],
        "manufacturers": dict(Counter(r["manufacturer"] for r in rows).most_common()),
        "manufacturer_links": db.manufacturer_links(rows),
        "related_codes": related,
    }


def update(conn: sqlite3.Connection, code: str, data: dict) -> dict:
    code = db.normalize_eccn(code)
    if not code:
        raise db.ValidationError("Missing ECCN code")
    conn.execute(
        "INSERT INTO eccn_codes (code, title, notes, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(code) DO UPDATE SET title = excluded.title, notes = excluded.notes, updated_at = excluded.updated_at",
        (code, (data.get("title") or "").strip(), (data.get("notes") or "").strip(),
         datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
    )
    return get(conn, code)
