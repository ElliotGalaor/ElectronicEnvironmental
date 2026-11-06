"""Codes ECCN : explication, définition officielle et composants concernés.

Un ECCN (Export Control Classification Number) de la Commerce Control List américaine
(15 CFR partie 774, supplément n° 1) se lit ainsi, par exemple 5A992.c :
  - 5     : catégorie (télécommunications et sécurité de l'information) ;
  - A     : groupe de produits (systèmes, équipements et composants) ;
  - 992   : numéro d'entrée ; sa centaine indique le motif de contrôle (9xx : contrôles
            unilatéraux américains, principalement anti-terrorisme) ;
  - .c    : sous-rubrique de l'entrée.
EAR99 désigne un article soumis aux EAR mais absent de la liste (15 CFR 734.3(c)).

Les intitulés officiels des entrées proviennent du texte de l'eCFR (data/ccl_headings.json,
domaine public). Les explications par catégorie / groupe / motif sont un résumé.
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
    "0": ("Matières nucléaires, installations, équipements et divers",
          "Matières et équipements nucléaires, ainsi que divers articles (armes à feu, articles militaires dits « 600 series »…)."),
    "1": ("Matériaux spéciaux, produits chimiques, micro-organismes et toxines",
          "Matériaux composites, produits chimiques, agents biologiques et toxines, et équipements associés."),
    "2": ("Traitement des matériaux",
          "Machines-outils, robots, équipements de fabrication et de traitement des matériaux."),
    "3": ("Électronique",
          "Composants et équipements électroniques : circuits intégrés, composants discrets, équipements de test et de fabrication de semi-conducteurs."),
    "4": ("Ordinateurs",
          "Ordinateurs, assemblages électroniques et équipements associés."),
    "5": ("Télécommunications et sécurité de l'information",
          "Partie 1 : équipements de télécommunications. Partie 2 : sécurité de l'information (cryptographie)."),
    "6": ("Capteurs et lasers",
          "Capteurs acoustiques, optiques, caméras, lasers, magnétomètres, radars."),
    "7": ("Navigation et avionique",
          "Systèmes de navigation, gyroscopes, accéléromètres, avionique."),
    "8": ("Marine", "Navires, sous-marins, systèmes et équipements marins."),
    "9": ("Aérospatiale et propulsion", "Moteurs, systèmes de propulsion, véhicules spatiaux, aéronefs et leurs composants."),
}

GROUPS = {
    "A": ("Systèmes, équipements et composants", "Le matériel lui-même : équipements, modules, composants."),
    "B": ("Équipements de test, d'inspection et de production", "Matériel servant à tester, inspecter ou fabriquer."),
    "C": ("Matériaux", "Matériaux et substances."),
    "D": ("Logiciels", "Logiciels liés aux articles de la catégorie."),
    "E": ("Technologie", "Technologie (savoir-faire, données techniques) de développement, production ou utilisation."),
}


def control_regime(number: int) -> tuple[str, str]:
    """Motif de contrôle indiqué par le numéro d'entrée (3 derniers chiffres)."""
    if number < 100:
        return ("Sécurité nationale (Arrangement de Wassenaar)",
                "Biens à double usage contrôlés au titre des accords multilatéraux : contrôle le plus strict, licence souvent requise hors pays alliés.")
    if number < 200:
        return ("Technologie des missiles (MTCR)", "Contrôle au titre du régime de contrôle de la technologie des missiles.")
    if number < 300:
        return ("Non-prolifération nucléaire (NSG)", "Contrôle au titre du Groupe des fournisseurs nucléaires.")
    if number < 400:
        return ("Armes chimiques et biologiques (Groupe d'Australie)", "Contrôle au titre du Groupe d'Australie.")
    if 500 <= number < 600:
        return ("Série « 500 » (véhicules spatiaux)", "Articles spatiaux transférés de la liste militaire américaine (USML).")
    if 600 <= number < 700:
        return ("Série « 600 » (articles militaires)", "Articles militaires transférés de la liste militaire américaine (USML) : contrôle strict.")
    if number >= 900:
        return ("Contrôles unilatéraux américains (9xx)",
                "Principalement anti-terrorisme (AT) : licence requise seulement pour un petit nombre de pays sous embargo ou de destinataires visés. Contrôle léger.")
    return ("Autre", "Motif de contrôle à vérifier dans la définition officielle.")


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
    """Explication d'un code ECCN (sans accès à la base)."""
    headings, as_of = _headings()
    if code == "EAR99":
        return {
            "code": "EAR99",
            "controlled": False,
            "summary": "Article soumis aux EAR mais ne figurant pas sur la Commerce Control List.",
            "explanation": "EAR99 désigne la grande majorité des produits commerciaux courants (composants passifs, circuits standards…).",
            "control_scope": ("Aucune licence n'est généralement requise, sauf exportation vers un pays sous embargo, "
                              "vers un destinataire faisant l'objet de restrictions ou pour un usage final interdit."),
            "parts": [],
            "official_heading": "",
            "links": [
                {"label": "Définition officielle : 15 CFR 734.3(c) (eCFR)", "url": EAR99_URL},
            ],
            "as_of": as_of,
        }
    m = _CODE_RE.match(code)
    if not m:
        return {"code": code, "controlled": True, "summary": "Code non reconnu", "explanation": "", "parts": [],
                "official_heading": "", "links": [{"label": "Commerce Control List (eCFR)", "url": CCL_URL}], "as_of": as_of}
    cat, grp, num, sub = m.group(1), m.group(2), m.group(3), m.group(4)
    entry = f"{cat}{grp}{num}"
    cat_name, cat_desc = CATEGORIES[cat]
    grp_name, grp_desc = GROUPS[grp]
    reg_name, reg_desc = control_regime(int(num))
    heading = headings.get(entry, "")
    parts = [
        {"code": cat, "label": "Catégorie", "name": cat_name, "description": cat_desc},
        {"code": grp, "label": "Groupe de produits", "name": grp_name, "description": grp_desc},
        {"code": num, "label": "Motif de contrôle", "name": reg_name, "description": reg_desc},
    ]
    if sub:
        parts.append({"code": sub, "label": "Sous-rubrique", "name": f"paragraphe {sub.lstrip('.')} de l'entrée {entry}",
                      "description": "Précise l'article visé au sein de l'entrée : voir la liste « Items » de l'entrée dans la définition officielle."})
    return {
        "code": code,
        "entry": entry,
        "controlled": True,
        "summary": f"{cat_name} — {grp_name.lower()}",
        "explanation": (f"Entrée {entry} de la catégorie {cat} ({cat_name.lower()}), groupe {grp} "
                        f"({grp_name.lower()}), contrôlée au titre : {reg_name.lower()}."
                        + (f" La sous-rubrique {sub.lstrip('.')} désigne un article précis de cette entrée." if sub else "")),
        "control_scope": reg_desc,
        "parts": parts,
        "official_heading": heading,
        "links": [
            {"label": f"Définition officielle de l'entrée {entry} : Commerce Control List, 15 CFR partie 774 (eCFR)", "url": CCL_URL,
             "hint": f"Sur la page, recherchez « {entry} » (Ctrl+F)."},
            {"label": "Pays soumis à licence selon le motif : Commerce Country Chart, 15 CFR partie 738 (eCFR)", "url": COUNTRY_CHART_URL},
        ],
        "as_of": as_of,
    }


# ------------------------------------------------------------------ base de données

def _stored(conn: sqlite3.Connection, code: str) -> sqlite3.Row | None:
    return conn.execute("SELECT * FROM eccn_codes WHERE code = ?", (code,)).fetchone()


def list_all(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT eccn, manufacturer FROM components WHERE eccn != ''").fetchall()
    counts = Counter(r["eccn"] for r in rows)
    mfrs: dict[str, set] = {}
    for r in rows:
        mfrs.setdefault(r["eccn"], set()).add(r["manufacturer"])
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
            "manufacturers": sorted(mfrs.get(code, set())),
        })
    out.sort(key=lambda e: (not e["controlled"], e["code"]))
    return {"codes": out, "unknown_components": unknown}


def get(conn: sqlite3.Connection, code: str) -> dict:
    code = db.normalize_eccn(code)
    d = describe(code)
    rows = conn.execute("SELECT * FROM components WHERE eccn = ? ORDER BY manufacturer, mpn", (code,)).fetchall()
    # Variantes : 5A992 regroupe 5A992.c, etc.
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
        "related_codes": related,
    }


def update(conn: sqlite3.Connection, code: str, data: dict) -> dict:
    code = db.normalize_eccn(code)
    if not code:
        raise db.ValidationError("Code ECCN manquant")
    conn.execute(
        "INSERT INTO eccn_codes (code, title, notes, updated_at) VALUES (?, ?, ?, ?) "
        "ON CONFLICT(code) DO UPDATE SET title = excluded.title, notes = excluded.notes, updated_at = excluded.updated_at",
        (code, (data.get("title") or "").strip(), (data.get("notes") or "").strip(),
         datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
    )
    return get(conn, code)
