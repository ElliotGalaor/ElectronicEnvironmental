"""Moteur d'analyse : rapproche chaque ligne de BOM de la base et calcule la conformité."""

from __future__ import annotations

import difflib
import sqlite3
from collections import Counter
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .db import eccn_is_controlled, loose_mpn, normalize_manufacturer, normalize_mpn, row_to_dict
from .tables import BomLine, ColumnMapping

NORMS = ("rohs2", "rohs3", "reach")

# Qualité du rapprochement
MATCH_EXACT = "exact"            # MPN + fabricant identiques
MATCH_MPN = "mpn"                # MPN identique, fabricant absent de la BOM
MATCH_MPN_OTHER_MFR = "mpn_autre_fabricant"  # MPN identique, fabricant différent -> à vérifier
MATCH_LOOSE = "approchant"       # identique hors ponctuation (ex. "LM358-DR" vs "LM358DR")
MATCH_PREFIX = "partiel"         # préfixe commun (suffixe d'emballage différent) -> à vérifier
MATCH_AMBIGUOUS = "ambigu"       # plusieurs candidats possibles
MATCH_NONE = "introuvable"

RELIABLE_MATCHES = {MATCH_EXACT, MATCH_MPN, MATCH_LOOSE}

# Verdict d'une ligne pour une norme
OK = "conforme"
OK_EXEMPTION = "exemption"   # RoHS : conforme avec exemption
SVHC = "svhc"                # REACH : SVHC > 0,1 % (obligation d'information)
KO = "non_conforme"
CHECK = "a_verifier"         # info manquante ou correspondance incertaine
SKIPPED = "non_monte"


@dataclass
class LineResult:
    line: int
    designator: str
    quantity: str
    bom_mpn: str
    bom_manufacturer: str
    bom_description: str
    dnp: bool
    match: str
    component: dict | None
    rohs2: str
    rohs3: str
    reach: str
    rohs2_inferred: bool = False
    rohs3_inferred: bool = False
    exemptions: str = ""
    svhc_list: str = ""
    suggestions: list[dict] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    original_mpn: str = ""
    original_manufacturer: str = ""


def _candidates(conn: sqlite3.Connection, line: BomLine) -> tuple[str, list[dict]]:
    mpn_n = normalize_mpn(line.mpn)
    mpn_l = loose_mpn(line.mpn)
    mfr_n = normalize_manufacturer(line.manufacturer)
    if not mpn_n:
        return MATCH_NONE, []

    def fetch(sql: str, params) -> list[dict]:
        return [row_to_dict(r) | {"_mfr_norm": r["manufacturer_norm"]} for r in conn.execute(sql, params)]

    def by_mfr(rows: list[dict]) -> list[dict]:
        return [r for r in rows if mfr_n and _mfr_equal(r["_mfr_norm"], mfr_n)]

    exact = fetch("SELECT * FROM components WHERE mpn_norm = ?", (mpn_n,))
    if exact:
        same = by_mfr(exact)
        if same:
            return MATCH_EXACT, same[:1]
        if not mfr_n:
            return (MATCH_MPN, exact) if len(exact) == 1 else (MATCH_AMBIGUOUS, exact)
        return MATCH_MPN_OTHER_MFR, exact

    if mpn_l:
        loose = fetch("SELECT * FROM components WHERE mpn_loose = ?", (mpn_l,))
        if loose:
            same = by_mfr(loose) or (loose if not mfr_n else [])
            if len(same) == 1:
                return MATCH_LOOSE, same
            return MATCH_AMBIGUOUS, loose

    # Préfixe : la BOM porte un suffixe d'emballage (ex. "-TR", "/NOPB") absent de la base, ou l'inverse.
    if len(mpn_l) >= 6:
        rows = fetch(
            "SELECT * FROM components WHERE (? LIKE mpn_loose || '%' AND length(mpn_loose) >= 6) "
            "OR (mpn_loose LIKE ? || '%')",
            (mpn_l, mpn_l),
        )
        if rows:
            same = by_mfr(rows) or (rows if not mfr_n else [])
            if same:
                same.sort(key=lambda r: -difflib.SequenceMatcher(None, mpn_l, loose_mpn(r["mpn"])).ratio())
                return MATCH_PREFIX, same[:5]

    return MATCH_NONE, []


def _mfr_equal(a: str, b: str) -> bool:
    if a == b:
        return True
    # Tolère "texas instruments" vs "texas instruments incorporated" etc.
    return bool(a and b) and (a.startswith(b + " ") or b.startswith(a + " "))


def _suggest(conn: sqlite3.Connection, mpn: str, all_mpns: list[tuple[int, str, str]], n: int = 3) -> list[dict]:
    target = loose_mpn(mpn)
    if not target:
        return []
    scored = []
    for cid, m, mfr in all_mpns:
        ratio = difflib.SequenceMatcher(None, target, loose_mpn(m)).ratio()
        if ratio >= 0.7:
            scored.append((ratio, cid, m, mfr))
    scored.sort(reverse=True)
    return [{"id": cid, "mpn": m, "manufacturer": mfr, "score": round(r, 2)} for r, cid, m, mfr in scored[:n]]


def _line_status(status: str, norm: str) -> str:
    if status == "conforme":
        return OK
    if status == "exemption":
        return OK_EXEMPTION
    if status == "svhc":
        return SVHC
    if status == "non_conforme":
        return KO
    return CHECK


def analyze_line(conn: sqlite3.Connection, line: BomLine, all_mpns) -> LineResult:
    res = LineResult(
        line=line.line,
        designator=line.designator,
        quantity=line.quantity,
        bom_mpn=line.mpn,
        bom_manufacturer=line.manufacturer,
        bom_description=line.description or line.value,
        dnp=line.dnp,
        match=MATCH_NONE,
        component=None,
        rohs2=CHECK,
        rohs3=CHECK,
        reach=CHECK,
        original_mpn=line.original_mpn,
        original_manufacturer=line.original_manufacturer,
    )
    if line.original_mpn:
        res.notes.append(f"Remplace {line.original_mpn}" + (f" ({line.original_manufacturer})" if line.original_manufacturer else ""))
    if line.dnp:
        res.rohs2 = res.rohs3 = res.reach = SKIPPED
        res.match = "-"
        res.notes.append("Composant non monté (DNP) : exclu de l'analyse")
        return res
    if not line.mpn:
        res.notes.append("Référence fabricant absente de la BOM")
        return res

    match, cands = _candidates(conn, line)
    res.match = match
    for c in cands:
        c.pop("_mfr_norm", None)

    if match == MATCH_NONE:
        res.notes.append("Référence absente de la base")
        res.suggestions = _suggest(conn, line.mpn, all_mpns)
        return res
    if match == MATCH_AMBIGUOUS:
        res.notes.append("Plusieurs composants correspondent : précisez le fabricant dans la BOM")
        res.suggestions = [{"id": c["id"], "mpn": c["mpn"], "manufacturer": c["manufacturer"]} for c in cands[:5]]
        return res

    comp = cands[0]
    res.component = comp
    statuses = {n: comp[n] for n in NORMS}

    # RoHS 3 (UE 2015/863) = RoHS 2 (2011/65/UE) + 4 phtalates : on peut déduire certains statuts.
    if statuses["rohs2"] == "inconnu" and statuses["rohs3"] in ("conforme", "exemption"):
        statuses["rohs2"] = statuses["rohs3"]
        res.rohs2_inferred = True
        res.notes.append("RoHS 2 déduit de la conformité RoHS 3")
    if statuses["rohs3"] == "inconnu" and statuses["rohs2"] == "non_conforme":
        statuses["rohs3"] = "non_conforme"
        res.rohs3_inferred = True
        res.notes.append("RoHS 3 déduit de la non-conformité RoHS 2")

    for n in NORMS:
        setattr(res, n, _line_status(statuses[n], n))
    unknown = [label for n, label in (("rohs2", "RoHS 2"), ("rohs3", "RoHS 3"), ("reach", "REACH")) if statuses[n] == "inconnu"]
    if unknown:
        res.notes.append(f"Statut inconnu dans la base : {', '.join(unknown)}")
    res.exemptions = comp.get("rohs_exemptions", "")
    res.svhc_list = comp.get("svhc_list", "")

    if match not in RELIABLE_MATCHES:
        # Correspondance incertaine : les statuts "conformes" deviennent "à vérifier".
        for n in NORMS:
            if getattr(res, n) in (OK, OK_EXEMPTION, SVHC):
                setattr(res, n, CHECK)
        if match == MATCH_MPN_OTHER_MFR:
            res.notes.append(
                f"Référence trouvée mais pour un autre fabricant ({comp['manufacturer']}) : vérifier la source"
            )
        elif match == MATCH_PREFIX:
            res.notes.append(f"Correspondance partielle avec {comp['mpn']} (suffixe différent) : vérifier")
        res.suggestions = [{"id": c["id"], "mpn": c["mpn"], "manufacturer": c["manufacturer"]} for c in cands[:5]]
    if res.exemptions and OK_EXEMPTION in (res.rohs2, res.rohs3):
        res.notes.append(f"Exemption RoHS : {res.exemptions} (vérifier sa date d'expiration)")
    if eccn_is_controlled(comp.get("eccn", "")):
        res.notes.append(f"ECCN {comp['eccn']} : composant soumis au contrôle export, vérifier les licences")
    if res.reach == SVHC:
        res.notes.append("Contient une SVHC > 0,1 % : obligation d'information (REACH art. 33) et déclaration SCIP")
    return res


def _global_verdict(values: list[str], norm: str) -> str:
    counted = [v for v in values if v != SKIPPED]
    if not counted:
        return CHECK
    if KO in counted:
        return KO
    if CHECK in counted:
        return CHECK
    if norm == "reach" and SVHC in counted:
        return SVHC
    if OK_EXEMPTION in counted:
        return OK_EXEMPTION
    return OK


def analyze(conn: sqlite3.Connection, lines: list[BomLine], mapping: ColumnMapping | None = None, filename: str = "") -> dict:
    all_mpns = [(r["id"], r["mpn"], r["manufacturer"]) for r in conn.execute("SELECT id, mpn, manufacturer FROM components")]
    results = [analyze_line(conn, l, all_mpns) for l in lines]

    summary = {}
    for n in NORMS:
        vals = [getattr(r, n) for r in results]
        summary[n] = {"verdict": _global_verdict(vals, n), "counts": dict(Counter(vals))}
    matches = Counter(r.match for r in results)

    # Export / douane : sur les composants montés et identifiés en base
    known = [r.component for r in results if r.component and not r.dnp]
    export = {
        "eccn_controlled": sum(1 for c in known if eccn_is_controlled(c.get("eccn", ""))),
        "eccn_ear99": sum(1 for c in known if c.get("eccn") == "EAR99"),
        "eccn_unknown": sum(1 for c in known if not c.get("eccn")),
        "countries": dict(Counter(c["country_of_origin"] for c in known if c.get("country_of_origin")).most_common()),
        "country_unknown": sum(1 for c in known if not c.get("country_of_origin")),
    }
    overall_values = [summary[n]["verdict"] for n in NORMS]
    overall = KO if KO in overall_values else CHECK if CHECK in overall_values else OK

    return {
        "filename": filename,
        "analyzed_at": datetime.now(timezone.utc).replace(microsecond=0).isoformat(),
        "database_size": len(all_mpns),
        "mapping": mapping.to_dict() if mapping else None,
        "summary": {
            "lines": len(results),
            "dnp": sum(r.dnp for r in results),
            "found": sum(1 for r in results if r.match in RELIABLE_MATCHES),
            "uncertain": sum(1 for r in results if r.match in (MATCH_MPN_OTHER_MFR, MATCH_PREFIX, MATCH_AMBIGUOUS)),
            "not_found": matches.get(MATCH_NONE, 0),
            "substituted": sum(1 for r in results if r.original_mpn),
            "overall": overall,
            "export": export,
            **summary,
        },
        "lines": [asdict(r) for r in results],
    }
