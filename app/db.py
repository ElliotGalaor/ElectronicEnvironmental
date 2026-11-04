"""Base de données des composants (SQLite).

Chaque composant est identifié par sa référence fabricant (MPN) et son fabricant.
Les statuts de conformité possibles sont :

RoHS 2 / RoHS 3 :
  - "conforme"      : conforme sans exemption
  - "exemption"     : conforme grâce à une exemption (ex. 7(c)-I), à préciser
  - "non_conforme"  : non conforme
  - "inconnu"       : pas d'information

REACH :
  - "conforme"      : aucune SVHC > 0,1 % m/m
  - "svhc"          : contient une ou plusieurs SVHC > 0,1 % (obligation d'info art. 33)
  - "non_conforme"  : substance restreinte (annexe XVII) ou non conformité déclarée
  - "inconnu"       : pas d'information
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DB_PATH = Path(os.environ.get("RRC_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "components.db"))

ROHS_STATUSES = ("conforme", "exemption", "non_conforme", "inconnu")
REACH_STATUSES = ("conforme", "svhc", "non_conforme", "inconnu")

FIELDS = (
    "mpn",
    "manufacturer",
    "description",
    "rohs2",
    "rohs3",
    "reach",
    "rohs_exemptions",
    "svhc_list",
    "source",
    "last_checked",
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS components (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    mpn               TEXT NOT NULL,
    mpn_norm          TEXT NOT NULL,
    mpn_loose         TEXT NOT NULL,
    manufacturer      TEXT NOT NULL DEFAULT '',
    manufacturer_norm TEXT NOT NULL DEFAULT '',
    description       TEXT NOT NULL DEFAULT '',
    rohs2             TEXT NOT NULL DEFAULT 'inconnu',
    rohs3             TEXT NOT NULL DEFAULT 'inconnu',
    reach             TEXT NOT NULL DEFAULT 'inconnu',
    rohs_exemptions   TEXT NOT NULL DEFAULT '',
    svhc_list         TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT '',
    last_checked      TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL,
    UNIQUE (mpn_norm, manufacturer_norm)
);
CREATE INDEX IF NOT EXISTS idx_components_mpn_norm  ON components (mpn_norm);
CREATE INDEX IF NOT EXISTS idx_components_mpn_loose ON components (mpn_loose);
"""

# Alias courants de fabricants -> forme canonique (après normalisation).
MANUFACTURER_ALIASES = {
    "ti": "texas instruments",
    "texas": "texas instruments",
    "texas instrument": "texas instruments",
    "st": "stmicroelectronics",
    "stm": "stmicroelectronics",
    "st microelectronics": "stmicroelectronics",
    "adi": "analog devices",
    "analog": "analog devices",
    "nxp semiconductors": "nxp",
    "on semi": "onsemi",
    "on semiconductor": "onsemi",
    "fairchild": "onsemi",
    "microchip technology": "microchip",
    "atmel": "microchip",
    "murata electronics": "murata",
    "murata manufacturing": "murata",
    "tdk corporation": "tdk",
    "yageo corporation": "yageo",
    "vishay intertechnology": "vishay",
    "vishay dale": "vishay",
    "vishay siliconix": "vishay",
    "infineon technologies": "infineon",
    "kemet electronics": "kemet",
    "samsung electro mechanics": "samsung",
    "samsung electromechanics": "samsung",
    "wurth": "wurth elektronik",
    "wurth electronics": "wurth elektronik",
    "we": "wurth elektronik",
    "te": "te connectivity",
    "tyco": "te connectivity",
    "tyco electronics": "te connectivity",
    "nexperia usa": "nexperia",
    "diodes incorporated": "diodes",
    "diodes inc": "diodes",
    "rohm semiconductor": "rohm",
    "panasonic electronic components": "panasonic",
    "bourns inc": "bourns",
    "molex llc": "molex",
}

_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|gmbh|ag|sa|sas|nv|bv|plc|kk|semiconductors?|technologies|technology|electronics?)\b"
)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_mpn(mpn: str | None) -> str:
    """Majuscules, sans espaces. On garde tirets, slash, # (ex. /NOPB, #PBF) car significatifs."""
    return re.sub(r"\s+", "", (mpn or "")).upper()


def loose_mpn(mpn: str | None) -> str:
    """Forme permissive : uniquement lettres et chiffres."""
    return re.sub(r"[^A-Z0-9]", "", normalize_mpn(mpn))


def normalize_manufacturer(name: str | None) -> str:
    if not name:
        return ""
    n = strip_accents(name).lower()
    n = n.replace("&", " and ")
    n = re.sub(r"[^a-z0-9 ]+", " ", n)
    n = re.sub(r"\s+", " ", n).strip()
    if n in MANUFACTURER_ALIASES:
        return MANUFACTURER_ALIASES[n]
    stripped = re.sub(r"\s+", " ", _SUFFIX_RE.sub(" ", n)).strip() or n
    return MANUFACTURER_ALIASES.get(stripped, stripped)


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


@contextmanager
def session(path: Path | str | None = None) -> Iterator[sqlite3.Connection]:
    conn = connect(path)
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


class ValidationError(ValueError):
    pass


def clean_status(value: str | None, allowed: tuple[str, ...], field: str) -> str:
    """Accepte des valeurs variées (Oui/Yes/Compliant/Non...) et les ramène à l'énumération."""
    v = strip_accents((value or "").strip().lower()).replace("-", "_").replace(" ", "_")
    if v in ("", "?", "n/a", "na", "unknown", "inconnu", "nc", "tbd", "a_verifier"):
        return "inconnu"
    synonyms = {
        "conforme": ("oui", "yes", "y", "ok", "compliant", "conforme", "c", "1", "true", "rohs", "reach"),
        "exemption": ("exemption", "exempt", "compliant_with_exemption", "conforme_avec_exemption", "exempte"),
        "svhc": ("svhc", "contains_svhc", "contient_svhc", "svhc>0.1%", "art33", "article_33"),
        "non_conforme": ("non", "no", "n", "non_conforme", "nonconforme", "not_compliant", "non_compliant", "noncompliant", "0", "false", "nok"),
    }
    for status, words in synonyms.items():
        if v in words and status in allowed:
            return status
    raise ValidationError(f"Statut invalide pour {field!r} : {value!r} (attendu : {', '.join(allowed)})")


def prepare(data: dict) -> dict:
    """Valide et normalise un enregistrement composant."""
    mpn = (data.get("mpn") or "").strip()
    if not mpn:
        raise ValidationError("La référence fabricant (mpn) est obligatoire")
    rec = {f: str(data.get(f) or "").strip() for f in FIELDS}
    rec["mpn"] = mpn
    rec["rohs2"] = clean_status(rec["rohs2"], ROHS_STATUSES, "rohs2")
    rec["rohs3"] = clean_status(rec["rohs3"], ROHS_STATUSES, "rohs3")
    rec["reach"] = clean_status(rec["reach"], REACH_STATUSES, "reach")
    rec["mpn_norm"] = normalize_mpn(mpn)
    rec["mpn_loose"] = loose_mpn(mpn)
    rec["manufacturer_norm"] = normalize_manufacturer(rec["manufacturer"])
    rec["updated_at"] = _now()
    return rec


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    for k in ("mpn_norm", "mpn_loose", "manufacturer_norm"):
        d.pop(k, None)
    return d


def upsert(conn: sqlite3.Connection, data: dict) -> tuple[int, bool]:
    """Insère ou met à jour (clé : MPN + fabricant). Retourne (id, créé)."""
    rec = prepare(data)
    existing = conn.execute(
        "SELECT id FROM components WHERE mpn_norm = ? AND manufacturer_norm = ?",
        (rec["mpn_norm"], rec["manufacturer_norm"]),
    ).fetchone()
    if existing:
        update(conn, existing["id"], data)
        return existing["id"], False
    cols = list(rec.keys())
    cur = conn.execute(
        f"INSERT INTO components ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
        [rec[c] for c in cols],
    )
    return cur.lastrowid, True


def create(conn: sqlite3.Connection, data: dict) -> int:
    rec = prepare(data)
    cols = list(rec.keys())
    try:
        cur = conn.execute(
            f"INSERT INTO components ({', '.join(cols)}) VALUES ({', '.join('?' for _ in cols)})",
            [rec[c] for c in cols],
        )
    except sqlite3.IntegrityError as exc:
        raise ValidationError("Ce composant (référence + fabricant) existe déjà") from exc
    return cur.lastrowid


def update(conn: sqlite3.Connection, comp_id: int, data: dict) -> bool:
    rec = prepare(data)
    sets = ", ".join(f"{c} = ?" for c in rec)
    try:
        cur = conn.execute(f"UPDATE components SET {sets} WHERE id = ?", [*rec.values(), comp_id])
    except sqlite3.IntegrityError as exc:
        raise ValidationError("Un autre composant a déjà cette référence + fabricant") from exc
    return cur.rowcount > 0


def delete(conn: sqlite3.Connection, comp_id: int) -> bool:
    return conn.execute("DELETE FROM components WHERE id = ?", (comp_id,)).rowcount > 0


def get(conn: sqlite3.Connection, comp_id: int) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM components WHERE id = ?", (comp_id,)).fetchone())


def search(conn: sqlite3.Connection, q: str = "", limit: int = 50, offset: int = 0, status: str = "") -> tuple[list[dict], int]:
    where, params = [], []
    if q:
        like = f"%{q.strip()}%"
        where.append("(mpn LIKE ? OR manufacturer LIKE ? OR description LIKE ? OR mpn_loose LIKE ?)")
        params += [like, like, like, f"%{loose_mpn(q)}%"]
    if status == "probleme":
        where.append("(rohs2 IN ('non_conforme','inconnu') OR rohs3 IN ('non_conforme','inconnu') OR reach IN ('non_conforme','inconnu','svhc'))")
    clause = f"WHERE {' AND '.join(where)}" if where else ""
    total = conn.execute(f"SELECT COUNT(*) FROM components {clause}", params).fetchone()[0]
    rows = conn.execute(
        f"SELECT * FROM components {clause} ORDER BY manufacturer, mpn LIMIT ? OFFSET ?",
        [*params, limit, offset],
    ).fetchall()
    return [row_to_dict(r) for r in rows], total


def all_components(conn: sqlite3.Connection) -> list[dict]:
    return [row_to_dict(r) for r in conn.execute("SELECT * FROM components ORDER BY manufacturer, mpn")]


def stats(conn: sqlite3.Connection) -> dict:
    out = {"total": conn.execute("SELECT COUNT(*) FROM components").fetchone()[0]}
    for field in ("rohs2", "rohs3", "reach"):
        out[field] = {r[0]: r[1] for r in conn.execute(f"SELECT {field}, COUNT(*) FROM components GROUP BY {field}")}
    return out
