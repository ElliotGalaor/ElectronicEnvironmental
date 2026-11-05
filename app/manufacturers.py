"""Fabricants : informations (site web, pays du siège, notes) et composants associés.

Un fabricant est identifié par son nom normalisé (le même que components.manufacturer_norm),
ce qui regroupe « TI », « Texas Instruments » et « Texas Instruments Inc. ».
Les informations saisies sont stockées dans la table `manufacturers` ; pour les fabricants
connus, un site web et un pays par défaut sont proposés tant que rien n'a été saisi.
"""

from __future__ import annotations

import re
import sqlite3
from collections import Counter
from datetime import datetime, timezone

from . import db
from .datasheet import MANUFACTURERS as KNOWN_MANUFACTURERS

# Pays du siège social des fabricants connus (code ISO 3166-1 alpha-2)
_HQ_COUNTRY = {
    "Texas Instruments": "US", "STMicroelectronics": "CH", "Analog Devices": "US", "Microchip": "US",
    "NXP": "NL", "onsemi": "US", "Infineon": "DE", "Renesas": "JP", "Nexperia": "NL", "Diodes": "US",
    "Vishay": "US", "ROHM": "JP", "Toshiba": "JP", "Murata": "JP", "TDK": "JP", "Samsung": "KR",
    "Yageo": "TW", "KEMET": "US", "Panasonic": "JP", "Wurth Elektronik": "DE", "TE Connectivity": "CH",
    "Molex": "US", "Amphenol": "US", "Bourns": "US", "Littelfuse": "US", "Abracon": "US", "Lite-On": "TW",
    "Kingbright": "TW", "Espressif": "CN", "Nordic Semiconductor": "NO", "Silicon Labs": "US",
    "Coilcraft": "US", "Bel Fuse": "US", "Hirose": "JP", "JST": "JP", "Phoenix Contact": "DE", "Omron": "JP",
}

# Valeurs par défaut : clé normalisée -> {name, website, country}
DEFAULTS = {
    db.normalize_manufacturer(name): {
        "name": name,
        "website": f"https://www.{domains[0]}" if domains else "",
        "country": _HQ_COUNTRY.get(name, ""),
    }
    for name, (_aliases, domains) in KNOWN_MANUFACTURERS.items()
}

_URL_RE = re.compile(r"^https?://[\w.-]+\.[a-z]{2,}(/\S*)?$", re.I)


def normalize_website(value: str | None) -> str:
    v = (value or "").strip()
    if not v:
        return ""
    if not re.match(r"^https?://", v, re.I):
        v = "https://" + v
    if not _URL_RE.match(v):
        raise db.ValidationError(f"Adresse de site web invalide : {value!r}")
    return v


def _stats(rows: list[sqlite3.Row]) -> dict:
    total = len(rows)
    count = lambda field, *values: sum(1 for r in rows if r[field] in values)  # noqa: E731
    return {
        "components": total,
        "rohs3_ok": count("rohs3", "conforme", "exemption"),
        "rohs3_ko": count("rohs3", "non_conforme"),
        "rohs3_unknown": count("rohs3", "inconnu"),
        "reach_svhc": count("reach", "svhc"),
        "reach_ko": count("reach", "non_conforme"),
        "eccn_controlled": sum(1 for r in rows if db.eccn_is_controlled(r["eccn"])),
        "eccn_unknown": sum(1 for r in rows if not r["eccn"]),
    }


def _info(key: str, stored: sqlite3.Row | None, display_names: Counter) -> dict:
    default = DEFAULTS.get(key, {})
    name = (stored["name"] if stored and stored["name"] else "") or default.get("name") or (
        display_names.most_common(1)[0][0] if display_names else key)
    website = stored["website"] if stored and stored["website"] else default.get("website", "")
    country = stored["country"] if stored and stored["country"] else default.get("country", "")
    return {
        "key": key,
        "name": name,
        "website": website,
        "country": country,
        "country_name": db.country_name(country) if country else "",
        "notes": stored["notes"] if stored else "",
        "aliases": sorted(n for n in display_names if n != name),
        "website_is_default": bool(website) and not (stored and stored["website"]),
        "country_is_default": bool(country) and not (stored and stored["country"]),
    }


def list_all(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT manufacturer, manufacturer_norm, rohs3, reach, eccn FROM components").fetchall()
    groups: dict[str, list] = {}
    names: dict[str, Counter] = {}
    for r in rows:
        k = r["manufacturer_norm"]
        groups.setdefault(k, []).append(r)
        names.setdefault(k, Counter())[r["manufacturer"]] += 1
    stored = {r["key"]: r for r in conn.execute("SELECT * FROM manufacturers")}
    out = []
    for key in set(groups) | set(stored):
        out.append(_info(key, stored.get(key), names.get(key, Counter())) | {"stats": _stats(groups.get(key, []))})
    out.sort(key=lambda m: (m["key"] == "", m["name"].lower()))
    return out


def get(conn: sqlite3.Connection, key: str) -> dict | None:
    rows = conn.execute("SELECT * FROM components WHERE manufacturer_norm = ? ORDER BY mpn", (key,)).fetchall()
    stored = conn.execute("SELECT * FROM manufacturers WHERE key = ?", (key,)).fetchone()
    if not rows and not stored and key not in DEFAULTS:
        return None
    names = Counter(r["manufacturer"] for r in rows)
    info = _info(key, stored, names)
    info["stats"] = _stats(rows)
    info["components"] = [db.row_to_dict(r) for r in rows]
    countries = Counter(r["country_of_origin"] for r in rows if r["country_of_origin"])
    info["origins"] = dict(countries.most_common())
    return info


def update(conn: sqlite3.Connection, key: str, data: dict) -> dict:
    rec = {
        "name": (data.get("name") or "").strip(),
        "website": normalize_website(data.get("website")),
        "country": db.normalize_country(data.get("country")),
        "notes": (data.get("notes") or "").strip(),
        "updated_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
    }
    conn.execute(
        "INSERT INTO manufacturers (key, name, website, country, notes, updated_at) VALUES (?, ?, ?, ?, ?, ?) "
        "ON CONFLICT(key) DO UPDATE SET name = excluded.name, website = excluded.website, "
        "country = excluded.country, notes = excluded.notes, updated_at = excluded.updated_at",
        (key, rec["name"], rec["website"], rec["country"], rec["notes"], rec["updated_at"]),
    )
    return get(conn, key)
