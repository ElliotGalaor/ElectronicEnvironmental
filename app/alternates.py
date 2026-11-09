"""Searching for equivalent components from other manufacturers.

Two sources:
  1. equivalences confirmed by a user (component_equivalents table);
  2. a similarity computed from the description: component family, main value
     (Ω, F, H, Hz, V), package, rated voltage and current, dielectric, tolerance,
     channel type (MOSFET).

The computed suggestions are leads: the pinout, the detailed characteristics and the
footprint must be checked against the datasheet before replacing a component.
"""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass, field

from . import db

# ------------------------------------------------------------------ attribute extraction

# (family, pattern) — the first matching pattern wins: order matters.
# French words are kept so that descriptions written in French are understood too.
_CATEGORIES = [
    ("tvs", r"\btvs\b|transil|suppress"),
    ("schottky", r"schottky"),
    ("zener", r"z[ée]ner"),
    ("led", r"\bled\b"),
    ("diode", r"\bdiode|redresseur|rectifier"),
    ("mosfet", r"mosfet"),
    ("bjt", r"transistor bipolaire|bipolar transistor|\bbjt\b|\bnpn\b|\bpnp\b"),
    ("opamp", r"ampli[- ]?op|op[- ]?amp|amplificateur op[ée]rationnel|operational amplifier"),
    ("comparator", r"comparat"),
    ("ldo", r"\bldo\b|r[ée]gulateur lin[ée]aire|linear (voltage )?regulator"),
    ("dcdc", r"\bbuck\b|\bboost\b|convertisseur|converter|dc[- ]?dc|switching regulator"),
    ("ldo", r"r[ée]gulateur|regulator"),
    ("timer", r"\btimer\b|temporisateur|\b555\b"),
    ("mcu", r"\bmcu\b|microcontr[ôo]leur|microcontroller"),
    ("can", r"\bcan\b.{0,20}transceiver|transceiver.{0,20}\bcan\b"),
    ("transceiver", r"transceiver|[ée]metteur[- ]r[ée]cepteur"),
    ("crystal", r"quartz|crystal|cristal|r[ée]sonateur|resonator"),
    ("oscillator", r"oscillat"),
    ("inductor", r"inductance|inductor|\bself\b"),
    ("ferrite", r"ferrite"),
    ("resistor", r"r[ée]sistance|resistor|\bres\b|couche [ée]paisse|couche mince|thick film|thin film"),
    ("cap_tantalum", r"tantale|tantalum"),
    ("cap_alu", r"[ée]lectrolytique|electrolytic"),
    ("capacitor", r"condensateur|capacitor|\bcap\b|mlcc"),
    ("connector", r"connecteur|connector|header|barrette|embase|bornier|terminal block"),
    ("relay", r"relais|relay"),
    ("fuse", r"fusible|\bfuse\b|polyfuse|\bptc\b"),
    ("cable", r"c[âa]ble|\bcable\b|\bwire\b"),
]
_CATEGORY_RES = [(c, re.compile(p, re.I)) for c, p in _CATEGORIES]

CATEGORY_LABELS = {
    "tvs": "TVS diode", "schottky": "Schottky diode", "zener": "Zener diode", "led": "LED", "diode": "diode",
    "mosfet": "MOSFET", "bjt": "bipolar transistor", "opamp": "op-amp", "comparator": "comparator",
    "ldo": "linear regulator", "dcdc": "DC-DC converter", "timer": "timer", "mcu": "microcontroller",
    "can": "CAN transceiver", "transceiver": "transceiver", "crystal": "crystal", "oscillator": "oscillator",
    "inductor": "inductor", "ferrite": "ferrite", "resistor": "resistor", "capacitor": "ceramic capacitor",
    "cap_tantalum": "tantalum capacitor", "cap_alu": "electrolytic capacitor",
    "connector": "connector", "relay": "relay", "fuse": "fuse", "cable": "cable",
}

# Main value of each family (SI unit)
_PRIMARY = {"resistor": "Ω", "capacitor": "F", "cap_tantalum": "F", "cap_alu": "F", "inductor": "H", "crystal": "Hz",
            "oscillator": "Hz", "ldo": "V", "zener": "V"}

_PREFIX = {"p": 1e-12, "n": 1e-9, "µ": 1e-6, "μ": 1e-6, "u": 1e-6, "m": 1e-3, "": 1.0, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}
_VALUE_RE = re.compile(r"(?<![\w.,])(\d+(?:[.,]\d+)?)\s?([pnµμumkKMG]?)\s?(Ω|ohms?|F|H|Hz|V|A|W)(?![a-zA-Z])")
_PACKAGE_RE = re.compile(
    r"\b(01005|0201|0402|0603|0805|1206|1210|1812|2010|2512|"
    r"SOT-?23(?:-\d)?|SOT-?223|SOT-?89|SOT-?363|SOT-?563|SC-?70|SOD-?123F?|SOD-?323|SOD-?523|SOD-?923|"
    r"SMA|SMB|SMC|DO-?214A[ABC]|DO-?41|SOIC-?\d+|SO-?\d+|TSSOP-?\d+|MSOP-?\d+|VSSOP-?\d+|DIP-?\d+|PDIP-?\d+|"
    r"TO-?220|TO-?92|TO-?252|TO-?263|DPAK|D2PAK|QFN-?\d+|DFN-?\d+|LQFP-?\d+|TQFP-?\d+|"
    r"\d[.,]\d\s?x\s?\d[.,]\d(?:\s?mm)?)\b",
    re.I,
)
_DIELECTRIC_RE = re.compile(r"\b(X7R|X5R|X6S|X7S|X8R|C0G|NP0|Y5V|Z5U)\b", re.I)
_TOL_RE = re.compile(r"(?<![\d.,])(\d+(?:[.,]\d+)?)\s?%")
_CHANNEL_RE = re.compile(r"canal\s+([NP])\b|\b([NP])[- ]channel|\b([NP])[- ]ch\b", re.I)
_PINS_RE = re.compile(r"(\d+)\s?(?:pts|points|contacts|pins|broches|voies|ways|positions)\b", re.I)


def _num(s: str) -> float:
    return float(s.replace(",", "."))


@dataclass
class Attributes:
    category: str = ""
    values: dict[str, list[float]] = field(default_factory=dict)  # unit -> values found
    package: str = ""
    dielectric: str = ""
    tolerance: float | None = None
    channel: str = ""
    pins: int | None = None

    def primary(self) -> float | None:
        unit = _PRIMARY.get(self.category)
        vals = self.values.get(unit) if unit else None
        return vals[0] if vals else None

    def first(self, unit: str) -> float | None:
        vals = self.values.get(unit)
        return vals[0] if vals else None

    def max(self, unit: str) -> float | None:
        vals = self.values.get(unit)
        return max(vals) if vals else None

    def to_dict(self) -> dict:
        return {
            "category": self.category,
            "category_label": CATEGORY_LABELS.get(self.category, ""),
            "values": {u: [format_value(v, u) for v in vs] for u, vs in self.values.items()},
            "package": self.package,
            "dielectric": self.dielectric,
            "tolerance": self.tolerance,
            "channel": self.channel,
            "pins": self.pins,
        }


def format_value(v: float, unit: str) -> str:
    for p, f in (("G", 1e9), ("M", 1e6), ("k", 1e3), ("", 1.0), ("m", 1e-3), ("µ", 1e-6), ("n", 1e-9), ("p", 1e-12)):
        if abs(v) >= f * 0.999:
            n = v / f
            return f"{n:.3g} {p}{unit}"
    return f"{v:g} {unit}"


def extract(description: str, mpn: str = "") -> Attributes:
    text = f"{description or ''}"
    a = Attributes()
    for cat, rx in _CATEGORY_RES:
        if rx.search(text):
            a.category = cat
            break
    for m in _VALUE_RE.finditer(text):
        unit = m.group(3)
        unit = "Ω" if unit.lower().startswith("ohm") else unit
        prefix = m.group(2)
        if unit == "Hz" and prefix == "m":
            prefix = "M"  # "16 mHz" is almost always a case typo for MHz
        a.values.setdefault(unit, []).append(_num(m.group(1)) * _PREFIX[prefix])
    m = _PACKAGE_RE.search(text) or _PACKAGE_RE.search(mpn or "")
    if m:
        a.package = re.sub(r"[\s-]", "", m.group(1)).upper().replace(",", ".").replace("MM", "")
    m = _DIELECTRIC_RE.search(text)
    if m:
        a.dielectric = m.group(1).upper().replace("NP0", "C0G")
    m = _TOL_RE.search(text)
    if m:
        a.tolerance = _num(m.group(1))
    m = _CHANNEL_RE.search(text)
    if m:
        a.channel = next(g for g in m.groups() if g).upper()
    m = _PINS_RE.search(text)
    if m:
        a.pins = int(m.group(1))
    return a


# ------------------------------------------------------------------ similarity

# Families that can replace each other
_FAMILY = {"schottky": "diode", "zener": "diode", "cap_tantalum": "capacitor", "cap_alu": "capacitor"}

# Complex components: sharing the same type is not enough (an STM32 does not replace an ATmega).
# They are only offered once confirmed as equivalents.
NO_AUTO_MATCH = {"mcu", "dcdc", "transceiver"}


def _family(cat: str) -> str:
    return _FAMILY.get(cat, cat)


def _close(a: float, b: float, tol: float = 0.01) -> bool:
    return abs(a - b) <= tol * max(abs(a), abs(b))


def similarity(ref: Attributes, cand: Attributes) -> tuple[int, list[str], list[str]] | None:
    """Return (score, common points, differences), or None if the candidate is not suitable."""
    if not ref.category or not cand.category or ref.category in NO_AUTO_MATCH:
        return None
    if ref.category != cand.category and _family(ref.category) != _family(cand.category):
        return None
    score, same, diff = 30, [f"same type ({CATEGORY_LABELS.get(cand.category, cand.category)})"], []
    if ref.category != cand.category:
        score -= 10
        diff.append(f"related type ({CATEGORY_LABELS.get(cand.category, cand.category)})")

    # Main value: must be identical (a 10 kΩ resistor does not replace a 100 Ω one)
    unit = _PRIMARY.get(ref.category)
    rp, cp = ref.primary(), cand.primary()
    if unit and rp is not None and cp is not None:
        if not _close(rp, cp):
            return None
        score += 40
        same.append(f"same value ({format_value(rp, unit)})")
    elif unit and rp is not None:
        diff.append("value not given")

    if ref.channel and cand.channel:
        if ref.channel != cand.channel:
            return None
        score += 10
        same.append(f"{ref.channel}-channel")

    if ref.package and cand.package:
        if ref.package == cand.package:
            score += 25
            same.append(f"same package ({ref.package})")
        else:
            score -= 10
            diff.append(f"different package ({cand.package} instead of {ref.package})")

    # Rated voltage and current (other than the main value): the replacement must withstand at least as much
    for u, label, bonus, malus in (("V", "voltage", 10, 20), ("A", "current", 5, 10), ("W", "power", 5, 10)):
        if u == unit:
            continue
        rv, cv = ref.max(u), cand.max(u)
        if rv is None or cv is None:
            continue
        if _close(rv, cv):
            score += bonus
            same.append(f"same {label} ({format_value(rv, u)})")
        elif cv > rv:
            score += bonus // 2
            same.append(f"higher {label} ({format_value(cv, u)} ≥ {format_value(rv, u)})")
        else:
            score -= malus
            diff.append(f"lower {label} ({format_value(cv, u)} < {format_value(rv, u)})")

    if ref.dielectric and cand.dielectric:
        if ref.dielectric == cand.dielectric:
            score += 10
            same.append(f"same dielectric ({ref.dielectric})")
        else:
            score -= 10
            diff.append(f"different dielectric ({cand.dielectric} instead of {ref.dielectric})")
    if ref.tolerance is not None and cand.tolerance is not None:
        if cand.tolerance <= ref.tolerance:
            score += 5
            same.append(f"tolerance {cand.tolerance:g} %")
        else:
            score -= 5
            diff.append(f"looser tolerance ({cand.tolerance:g} % instead of {ref.tolerance:g} %)")
    if ref.pins and cand.pins:
        if ref.pins == cand.pins:
            score += 10
            same.append(f"{ref.pins} contacts")
        else:
            return None
    return score, same, diff


# ------------------------------------------------------------------ compliance (for sorting)

_ROHS_RANK = {"compliant": 0, "exemption": 1, "unknown": 2, "non_compliant": 3}
_REACH_RANK = {"compliant": 0, "svhc": 1, "unknown": 2, "non_compliant": 3}


def compliance_rank(c: dict) -> int:
    return _ROHS_RANK.get(c.get("rohs3"), 2) * 4 + _ROHS_RANK.get(c.get("rohs2"), 2) + _REACH_RANK.get(c.get("reach"), 2)


def is_fully_compliant(c: dict) -> bool:
    return (c.get("rohs2") in ("compliant", "exemption") and c.get("rohs3") in ("compliant", "exemption")
            and c.get("reach") in ("compliant", "svhc"))


# ------------------------------------------------------------------ confirmed equivalences

def confirmed_ids(conn: sqlite3.Connection, comp_id: int) -> set[int]:
    return {r[0] for r in conn.execute("SELECT b FROM component_equivalents WHERE a = ?", (comp_id,))}


def confirm(conn: sqlite3.Connection, a: int, b: int) -> None:
    if a == b:
        raise db.ValidationError("A component cannot be its own equivalent")
    if not db.get(conn, a) or not db.get(conn, b):
        raise db.ValidationError("Component not found")
    conn.executemany("INSERT OR IGNORE INTO component_equivalents (a, b) VALUES (?, ?)", [(a, b), (b, a)])


def unconfirm(conn: sqlite3.Connection, a: int, b: int) -> None:
    conn.execute("DELETE FROM component_equivalents WHERE (a = ? AND b = ?) OR (a = ? AND b = ?)", (a, b, b, a))


# ------------------------------------------------------------------ search

def find(conn: sqlite3.Connection, *, description: str = "", mpn: str = "", manufacturer: str = "",
         component_id: int | None = None, limit: int = 20) -> dict:
    """Equivalent components from other manufacturers, from closest to least close."""
    ref_comp = db.get(conn, component_id) if component_id else None
    if ref_comp:
        description, mpn, manufacturer = ref_comp["description"], ref_comp["mpn"], ref_comp["manufacturer"]
    ref = extract(description, mpn)
    ref_mfr = db.normalize_manufacturer(manufacturer)
    confirmed = confirmed_ids(conn, component_id) if component_id else set()

    out = []
    for row in conn.execute("SELECT * FROM components"):
        if component_id and row["id"] == component_id:
            continue
        is_confirmed = row["id"] in confirmed
        if not is_confirmed and ref_mfr and row["manufacturer_norm"] == ref_mfr:
            continue  # other manufacturers only
        cand = extract(row["description"], row["mpn"])
        sim = similarity(ref, cand)
        if sim is None and not is_confirmed:
            continue
        score, same, diff = sim if sim else (0, [], [])
        if is_confirmed:
            score += 100
        if score < 20:
            continue
        comp = db.row_to_dict(row)
        out.append({
            "component": comp,
            "score": score,
            "confirmed": is_confirmed,
            "matches": same,
            "differences": diff,
            "compliant": is_fully_compliant(comp),
        })
    out.sort(key=lambda a: (not a["confirmed"], -a["score"], compliance_rank(a["component"]), a["component"]["mpn"]))
    return {
        "reference": ref_comp or {"mpn": mpn, "manufacturer": manufacturer, "description": description},
        "attributes": ref.to_dict(),
        "alternatives": out[:limit],
    }
