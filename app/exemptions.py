"""RoHS exemptions: official text, expiry dates and affected components.

The exemptions are listed in Annexes III (all EEE categories) and IV (medical devices and
monitoring and control instruments) of Directive 2011/65/EU. The official English text is
extracted from the consolidated text published on EUR-Lex (data/rohs_exemptions.json, generated
by tools/extract_rohs_exemptions.py).

Notation: "7(c)-I", "7 c)-I" or "7c-I" refer to the same Annex III exemption;
prefix with "IV-" for Annex IV (e.g. "IV-37").
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
ART5_NOTE = ("Article 5(5): if an application for renewal was submitted no later than 18 months before "
             "expiry, the exemption remains valid until the Commission has taken a decision.")

_SUBSTANCES = [
    ("lead", r"\blead\b"), ("mercury", r"\bmercury\b"), ("cadmium", r"\bcadmium\b"),
    ("hexavalent chromium", r"hexavalent chromium"), ("PBB", r"\bPBB\b|polybrominated biphenyls"),
    ("PBDE", r"\bPBDE\b|polybrominated diphenyl"), ("phthalates (DEHP, BBP, DBP, DIBP)", r"phthalate|\bDEHP\b|\bBBP\b|\bDBP\b|\bDIBP\b"),
]


@lru_cache(maxsize=1)
def _data() -> dict:
    path = Path(__file__).resolve().parent.parent / "data" / "rohs_exemptions.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {"exemptions": {}}


def normalize_key(code: str) -> str:
    """"7(c)-I" -> "7C-I"; "Annex IV 37" / "IV-37" -> "IV-37"."""
    c = (code or "").strip()
    m = re.match(r"^(?:annexe\s+|annex\s+)?IV\s*[-:]?\s*(?:point\s+)?(.+)$", c, re.I)
    prefix = ""
    if m:
        prefix, c = "IV-", m.group(1)
    return prefix + re.sub(r"[\s().]", "", c).upper()


def split_codes(field: str) -> list[str]:
    """The rohs_exemptions field may hold several codes: "7(a), 7(c)-I"."""
    parts = re.split(r"[,;/]|\s+et\s+|\s+and\s+", field or "")
    return [p.strip() for p in parts if p.strip()]


def _substances(text: str) -> list[str]:
    return [name for name, rx in _SUBSTANCES if re.search(rx, text, re.I)]


def _status(dates: list[str]) -> dict:
    today = date.today().isoformat()
    if not dates:
        return {"kind": "no_date", "label": "No expiry date given", "next": ""}
    future = [d for d in dates if d >= today]
    if future:
        nxt = min(future)
        days = (date.fromisoformat(nxt) - date.today()).days
        return {"kind": "expiring_soon" if days <= 365 else "in_force",
                "label": f"Expires on {_long_date(nxt)}" + (f" (in {days} days)" if days <= 365 else ""), "next": nxt}
    last = max(dates)
    return {"kind": "expired", "label": f"Expiry date passed ({_long_date(last)}): check whether a renewal is pending",
            "next": last}


_MONTHS = ["January", "February", "March", "April", "May", "June", "July", "August", "September", "October",
           "November", "December"]


def _long_date(iso: str) -> str:
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
        links.append({"label": f"Official consolidated text of Directive 2011/65/EU, version of {data.get('version', '')} (EUR-Lex)",
                      "url": data["url"], "hint": f"Annex {e['annex'] if e else 'III'}, point {e['code'] if e else code}."})
    links.append({"label": "Directive 2011/65/EU, original text (EUR-Lex)", "url": DIRECTIVE_URL})
    base["links"] = links
    if not e:
        return base | {"found": False, "code": code, "annex": "", "description": "", "scope": "", "dates": [],
                       "status": {"kind": "unknown", "label": "Exemption not found in Annexes III and IV", "next": ""},
                       "substances": []}
    upcoming = data.get("upcoming") or {}
    change = (upcoming.get("changes") or {}).get(key)
    full = (e.get("parent", "") + " " + e["description"]).strip()
    return base | {
        "found": True,
        "code": e["code"],
        "annex": e["annex"],
        "annex_label": ("Annex III — exempted applications for all categories of equipment" if e["annex"] == "III"
                        else "Annex IV — medical devices and monitoring and control instruments"),
        "group": e.get("group", ""),
        "parent": e.get("parent", ""),
        "description": e["description"],
        "scope": e["scope"],
        "dates": e["dates"],
        "status": _status(e["dates"]),
        "substances": _substances(full),
        "upcoming": ({"version": upcoming.get("version"), "url": upcoming.get("url"), **change} if change else
                     {"version": upcoming.get("version"), "url": upcoming.get("url"), "type": "none"} if upcoming else None),
    }


# ------------------------------------------------------------------ database

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
        raise db.ValidationError("Missing exemption code")
    conn.execute(
        "INSERT INTO exemption_notes (key, notes, updated_at) VALUES (?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET notes = excluded.notes, updated_at = excluded.updated_at",
        (key, (notes or "").strip(), datetime.now(timezone.utc).replace(microsecond=0).isoformat()),
    )
    return get(conn, code)
