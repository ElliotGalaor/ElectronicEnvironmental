"""Exemptions RoHS : texte officiel, dates d'expiration et composants concernés.

Les exemptions figurent aux annexes III (toutes catégories d'EEE) et IV (dispositifs médicaux et
instruments de surveillance et de contrôle) de la directive 2011/65/UE. Le texte officiel, en
français, est extrait du texte consolidé publié sur EUR-Lex (data/rohs_exemptions.json, généré par
tools/extract_rohs_exemptions.py).

Notation : « 7(c)-I », « 7 c)-I » ou « 7c-I » désignent la même exemption de l'annexe III ;
préfixer par « IV- » pour l'annexe IV (ex. « IV-37 »).
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections import Counter
from datetime import date, datetime, timezone
from functools import lru_cache
from pathlib import Path

from . import db

DIRECTIVE_URL = "https://eur-lex.europa.eu/eli/dir/2011/65/oj"
ART5_NOTE = ("Article 5, paragraphe 5 : si une demande de renouvellement a été déposée au plus tard 18 mois avant "
             "l'expiration, l'exemption reste valable jusqu'à ce que la Commission ait statué.")

_SUBSTANCES = [
    ("plomb", r"\bplomb\b"), ("mercure", r"\bmercure\b"), ("cadmium", r"\bcadmium\b"),
    ("chrome hexavalent", r"chrome hexavalent"), ("PBB", r"\bPBB\b|polybromobiph[ée]nyles"),
    ("PBDE", r"\bPBDE\b|polybromodiph[ée]nyl"), ("phtalates (DEHP, BBP, DBP, DIBP)", r"phtalate|\bDEHP\b|\bBBP\b|\bDBP\b|\bDIBP\b"),
]


@lru_cache(maxsize=1)
def _data() -> dict:
    path = Path(__file__).resolve().parent.parent / "data" / "rohs_exemptions.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"exemptions": {}}


def normalize_key(code: str) -> str:
    """« 7(c)-I » -> « 7C-I » ; « Annexe IV 37 » / « IV-37 » -> « IV-37 »."""
    c = (code or "").strip()
    m = re.match(r"^(?:annexe\s+|annex\s+)?IV\s*[-:]?\s*(?:point\s+)?(.+)$", c, re.I)
    prefix = ""
    if m:
        prefix, c = "IV-", m.group(1)
    return prefix + re.sub(r"[\s().]", "", c).upper()


def split_codes(field: str) -> list[str]:
    """Le champ rohs_exemptions peut contenir plusieurs codes : « 7(a), 7(c)-I »."""
    parts = re.split(r"[,;/]|\s+et\s+|\s+and\s+", field or "")
    return [p.strip() for p in parts if p.strip()]


def _substances(text: str) -> list[str]:
    return [name for name, rx in _SUBSTANCES if re.search(rx, text, re.I)]


def _status(dates: list[str]) -> dict:
    today = date.today().isoformat()
    if not dates:
        return {"kind": "sans_date", "label": "Pas de date d'expiration indiquée", "next": ""}
    future = [d for d in dates if d >= today]
    if future:
        nxt = min(future)
        days = (date.fromisoformat(nxt) - date.today()).days
        return {"kind": "bientot" if days <= 365 else "en_vigueur",
                "label": f"Expire le {_fr(nxt)}" + (f" (dans {days} jours)" if days <= 365 else ""), "next": nxt}
    last = max(dates)
    return {"kind": "depassee", "label": f"Date d'expiration dépassée ({_fr(last)}) : vérifier si un renouvellement est en cours",
            "next": last}


_MONTHS = ["janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre"]


def _fr(iso: str) -> str:
    y, m, d = (int(x) for x in iso.split("-"))
    return f"{d} {_MONTHS[m - 1]} {y}"


def describe(code: str) -> dict:
    data = _data()
    key = normalize_key(code)
    e = data.get("exemptions", {}).get(key)
    base = {"key": key, "input": code, "version": data.get("version", ""), "source_url": data.get("url", ""),
            "links": [], "renewal_note": ART5_NOTE}
    links = []
    if data.get("url"):
        links.append({"label": f"Texte officiel consolidé de la directive 2011/65/UE, version du {data.get('version', '')} (EUR-Lex)",
                      "url": data["url"], "hint": f"Annexe {e['annex'] if e else 'III'}, point {e['code'] if e else code}."})
    links.append({"label": "Directive 2011/65/UE, texte d'origine (EUR-Lex)", "url": DIRECTIVE_URL})
    base["links"] = links
    if not e:
        return base | {"found": False, "code": code, "annex": "", "description": "", "scope": "", "dates": [],
                       "status": {"kind": "inconnue", "label": "Exemption introuvable dans les annexes III et IV", "next": ""},
                       "substances": []}
    upcoming = data.get("upcoming") or {}
    change = (upcoming.get("changes") or {}).get(key)
    full = (e.get("parent", "") + " " + e["description"]).strip()
    return base | {
        "found": True,
        "code": e["code"],
        "annex": e["annex"],
        "annex_label": ("Annexe III — applications exemptées pour toutes les catégories d'équipements" if e["annex"] == "III"
                        else "Annexe IV — dispositifs médicaux et instruments de surveillance et de contrôle"),
        "group": e.get("group", ""),
        "parent": e.get("parent", ""),
        "description": e["description"],
        "scope": e["scope"],
        "dates": e["dates"],
        "status": _status(e["dates"]),
        "substances": _substances(full),
        "upcoming": ({"version": upcoming.get("version"), "url": upcoming.get("url"), **change} if change else
                     {"version": upcoming.get("version"), "url": upcoming.get("url"), "type": "aucun"} if upcoming else None),
    }


# ------------------------------------------------------------------ base de données

def _components_by_key(conn: sqlite3.Connection) -> dict[str, list[sqlite3.Row]]:
    out: dict[str, list] = {}
    for r in conn.execute("SELECT * FROM components WHERE rohs_exemptions != '' ORDER BY manufacturer, mpn"):
        for c in split_codes(r["rohs_exemptions"]):
            out.setdefault(normalize_key(c), []).append(r)
    return out


def list_all(conn: sqlite3.Connection) -> dict:
    by_key = _components_by_key(conn)
    notes = {r["key"] for r in conn.execute("SELECT key FROM exemption_notes")}
    rows = []
    for key in set(by_key) | notes:
        d = describe(key)
        comps = by_key.get(key, [])
        rows.append({
            "key": key,
            "code": d["code"],
            "annex": d["annex"],
            "found": d["found"],
            "description": d["description"],
            "parent": d.get("parent", ""),
            "status": d["status"],
            "substances": d["substances"],
            "components": len(comps),
            "manufacturers": sorted({c["manufacturer"] for c in comps}),
        })
    rows.sort(key=lambda r: (not r["found"], r["annex"], [int(x) if x.isdigit() else x for x in re.findall(r"\d+|\D+", r["key"])]))
    data = _data()
    return {"exemptions": rows, "version": data.get("version", ""), "source_url": data.get("url", ""),
            "upcoming": {k: v for k, v in (data.get("upcoming") or {}).items() if k != "changes"} | {
                "changes": len((data.get("upcoming") or {}).get("changes") or {})} if data.get("upcoming") else None}


def get(conn: sqlite3.Connection, code: str) -> dict:
    d = describe(code)
    comps = _components_by_key(conn).get(d["key"], [])
    row = conn.execute("SELECT notes FROM exemption_notes WHERE key = ?", (d["key"],)).fetchone()
    return d | {
        "notes": row["notes"] if row else "",
        "components": [db.row_to_dict(c) for c in comps],
        "manufacturers": dict(Counter(c["manufacturer"] for c in comps).most_common()),
    }


def update(conn: sqlite3.Connection, code: str, notes: str) -> dict:
    key = normalize_key(code)
    if not key:
        raise db.ValidationError("Code d'exemption manquant")
    conn.execute(
        "INSERT INTO exemption_notes (key, notes, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET notes = excluded.notes, updated_at = excluded.updated_at",
        (key, (notes or "").strip(), datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
    )
    return get(conn, code)
