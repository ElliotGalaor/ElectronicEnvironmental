"""Component database (SQLite).

Each component is identified by its manufacturer part number (MPN) and its manufacturer.
Possible compliance statuses:

RoHS 2 / RoHS 3:
  - "compliant"      : compliant without exemption
  - "exemption"      : compliant thanks to an exemption (e.g. 7(c)-I), to be specified
  - "non_compliant"  : not compliant
  - "unknown"        : no information

REACH:
  - "compliant"      : no SVHC above 0.1 % w/w
  - "svhc"           : contains one or more SVHCs above 0.1 % (information duty, art. 33)
  - "non_compliant"  : restricted substance (Annex XVII) or declared non-compliance
  - "unknown"        : no information

Export / customs:
  - eccn              : Export Control Classification Number (US EAR / EU dual-use list),
                        e.g. "EAR99", "5A992.c", "3A991.a.1"; empty = unknown
  - country_of_origin : country of origin, ISO 3166-1 alpha-2 code (e.g. "CN", "MY", "DE"); empty = unknown
"""

from __future__ import annotations

import os
import re
import sqlite3
import unicodedata
from collections import Counter
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Iterator

DB_PATH = Path(os.environ.get("RRC_DB_PATH", Path(__file__).resolve().parent.parent / "data" / "components.db"))

ROHS_STATUSES = ("compliant", "exemption", "non_compliant", "unknown")
REACH_STATUSES = ("compliant", "svhc", "non_compliant", "unknown")

FIELDS = (
    "mpn",
    "manufacturer",
    "description",
    "rohs2",
    "rohs3",
    "reach",
    "rohs_exemptions",
    "svhc_list",
    "eccn",
    "country_of_origin",
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
    rohs2             TEXT NOT NULL DEFAULT 'unknown',
    rohs3             TEXT NOT NULL DEFAULT 'unknown',
    reach             TEXT NOT NULL DEFAULT 'unknown',
    rohs_exemptions   TEXT NOT NULL DEFAULT '',
    svhc_list         TEXT NOT NULL DEFAULT '',
    eccn              TEXT NOT NULL DEFAULT '',
    country_of_origin TEXT NOT NULL DEFAULT '',
    source            TEXT NOT NULL DEFAULT '',
    last_checked      TEXT NOT NULL DEFAULT '',
    updated_at        TEXT NOT NULL,
    UNIQUE (mpn_norm, manufacturer_norm)
);
CREATE INDEX IF NOT EXISTS idx_components_mpn_norm  ON components (mpn_norm);
CREATE INDEX IF NOT EXISTS idx_components_mpn_loose ON components (mpn_loose);
CREATE INDEX IF NOT EXISTS idx_components_mfr_norm  ON components (manufacturer_norm);

-- Equivalences confirmed by a user (stored in both directions)
CREATE TABLE IF NOT EXISTS component_equivalents (
    a INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    b INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    PRIMARY KEY (a, b)
);

-- User accounts (password hashed with scrypt, never stored in clear text)
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    -- Right to change the component database, granted by the administrator: '', pending, approved, refused
    edit_access       TEXT NOT NULL DEFAULT '',
    edit_token_hash   TEXT,   -- SHA-256 of the token in the administrator's review link
    edit_requested_at TEXT,
    edit_reviewed_at  TEXT
);

-- Components added by each user, for the daily limit
CREATE TABLE IF NOT EXISTS additions (
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    count      INTEGER NOT NULL,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_additions_user ON additions (user_id, created_at);

-- Login sessions: only the SHA-256 hash of the token is kept
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);

-- Sign-ups waiting for e-mail confirmation: one row per attempt, with the password chosen
-- in that attempt. Only the SHA-256 hash of the link's token is kept.
CREATE TABLE IF NOT EXISTS signup_requests (
    token_hash    TEXT PRIMARY KEY,
    email         TEXT NOT NULL,
    name          TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    expires_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_signup_requests_email ON signup_requests (email);

-- History of the BOMs analysed by each user (original file kept so it can be reopened)
CREATE TABLE IF NOT EXISTS bom_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    file_data     BLOB NOT NULL,
    mapping       TEXT NOT NULL DEFAULT '',
    substitutions TEXT NOT NULL DEFAULT '',
    classifications TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bom_history_user ON bom_history (user_id, updated_at);

-- RoHS exemptions: notes entered by the user (the official text comes from data/rohs_exemptions.json)
CREATE TABLE IF NOT EXISTS exemption_notes (
    key        TEXT PRIMARY KEY,
    notes      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- ECCN codes: title and notes entered by the user (the explanation is computed)
CREATE TABLE IF NOT EXISTS eccn_codes (
    code       TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Manufacturer information (key = normalised name, like components.manufacturer_norm)
CREATE TABLE IF NOT EXISTS manufacturers (
    key        TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    website    TEXT NOT NULL DEFAULT '',
    country    TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
"""

# Common manufacturer aliases -> canonical form (after normalisation).
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
    "liteon": "lite on",
    "lite on technology": "lite on",
    "murata manufacturing co": "murata",
    "molex llc": "molex",
}

_SUFFIX_RE = re.compile(
    r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|gmbh|ag|sa|sas|nv|bv|plc|kk|semiconductors?|technologies|technology|electronics?)\b"
)


def strip_accents(text: str) -> str:
    return "".join(c for c in unicodedata.normalize("NFKD", text) if not unicodedata.combining(c))


def normalize_mpn(mpn: str | None) -> str:
    """Upper case, no spaces. Dashes, slashes and # are kept (e.g. /NOPB, #PBF) because they are significant."""
    return re.sub(r"\s+", "", (mpn or "")).upper()


def loose_mpn(mpn: str | None) -> str:
    """Permissive form: letters and digits only."""
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


# Columns added after the first version: added to existing databases when they are opened.
_ADDED_COLUMNS = {
    "eccn": "TEXT NOT NULL DEFAULT ''",
    "country_of_origin": "TEXT NOT NULL DEFAULT ''",
}
_ADDED_USER_COLUMNS = {
    "edit_access": "TEXT NOT NULL DEFAULT ''",
    "edit_token_hash": "TEXT",
    "edit_requested_at": "TEXT",
    "edit_reviewed_at": "TEXT",
}

# Status values used up to v1.1.0 (French) -> current values.
LEGACY_STATUSES = {
    "conforme": "compliant",
    "non_conforme": "non_compliant",
    "inconnu": "unknown",
    "a_verifier": "needs_review",
    "non_monte": "dnp",
}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(components)")}
    for col, decl in _ADDED_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE components ADD COLUMN {col} {decl}")
    existing = {r[1] for r in conn.execute("PRAGMA table_info(users)")}
    for col, decl in _ADDED_USER_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE users ADD COLUMN {col} {decl}")
    if "classifications" not in {r[1] for r in conn.execute("PRAGMA table_info(bom_history)")}:
        conn.execute("ALTER TABLE bom_history ADD COLUMN classifications TEXT NOT NULL DEFAULT ''")
    # Databases created before v2.0.0 store French status values.
    legacy = ", ".join(f"'{s}'" for s in LEGACY_STATUSES)
    if conn.execute(f"SELECT 1 FROM components WHERE rohs2 IN ({legacy}) OR rohs3 IN ({legacy}) "
                    f"OR reach IN ({legacy}) LIMIT 1").fetchone():
        for field in ("rohs2", "rohs3", "reach"):
            for old, new in LEGACY_STATUSES.items():
                conn.execute(f"UPDATE components SET {field} = ? WHERE {field} = ?", (new, old))
    conn.commit()


# ------------------------------------------------------------------ ECCN and country of origin

_ECCN_RE = re.compile(r"^(\d)([A-E])(\d{3})((?:\.[A-Z0-9]+)*)$", re.I)
_EMPTY_VALUES = ("", "?", "na", "n/a", "inconnu", "unknown", "tbd", "nc")


def normalize_eccn(value: str | None) -> str:
    """Normalise an ECCN: "5a992.C" -> "5A992.c", "ear 99" -> "EAR99". Empty = unknown."""
    v = re.sub(r"\s+", "", (value or "")).strip().rstrip(".")
    if v.lower() in _EMPTY_VALUES:
        return ""
    if v.upper() == "EAR99":
        return "EAR99"
    m = _ECCN_RE.match(v)
    if not m:
        raise ValidationError(f"Invalid ECCN: {value!r} (expected EAR99 or a code such as 5A992.c, 3A001.a.1)")
    return f"{m.group(1)}{m.group(2).upper()}{m.group(3)}{m.group(4).lower()}"


def eccn_is_controlled(eccn: str) -> bool:
    """True for an ECCN on the Commerce Control List (anything except EAR99 and empty)."""
    return bool(eccn) and eccn != "EAR99"


# ISO code -> English name, then aliases (French names, variants) -> code
COUNTRIES = {
    "AT": "Austria", "AU": "Australia", "BE": "Belgium", "BG": "Bulgaria", "BR": "Brazil", "CA": "Canada",
    "CH": "Switzerland", "CN": "China", "CR": "Costa Rica", "CZ": "Czechia", "DE": "Germany", "DK": "Denmark",
    "EE": "Estonia", "ES": "Spain", "FI": "Finland", "FR": "France", "GB": "United Kingdom", "GR": "Greece",
    "HK": "Hong Kong", "HU": "Hungary", "ID": "Indonesia", "IE": "Ireland", "IL": "Israel", "IN": "India",
    "IT": "Italy", "JP": "Japan", "KR": "South Korea", "LT": "Lithuania", "LU": "Luxembourg", "LV": "Latvia",
    "MA": "Morocco", "MT": "Malta", "MX": "Mexico", "MY": "Malaysia", "NL": "Netherlands", "NO": "Norway",
    "PH": "Philippines", "PL": "Poland", "PT": "Portugal", "RO": "Romania", "RS": "Serbia", "RU": "Russia",
    "SE": "Sweden", "SG": "Singapore", "SI": "Slovenia", "SK": "Slovakia", "TH": "Thailand", "TN": "Tunisia",
    "TR": "Turkey", "TW": "Taiwan", "UA": "Ukraine", "US": "United States", "VN": "Vietnam", "ZA": "South Africa",
}
_COUNTRY_ALIASES = {
    # English variants
    "prc": "CN", "people s republic of china": "CN", "czech republic": "CZ", "uk": "GB", "great britain": "GB",
    "england": "GB", "uk great britain": "GB", "korea": "KR", "republic of korea": "KR", "the netherlands": "NL",
    "holland": "NL", "russian federation": "RU", "turkiye": "TR", "republic of china": "TW", "chinese taipei": "TW",
    "usa": "US", "united states of america": "US", "viet nam": "VN",
    # French names (files exported by French-language tools)
    "autriche": "AT", "australie": "AU", "belgique": "BE", "bulgarie": "BG", "bresil": "BR", "suisse": "CH",
    "chine": "CN", "tchequie": "CZ", "republique tcheque": "CZ", "allemagne": "DE", "danemark": "DK",
    "estonie": "EE", "espagne": "ES", "finlande": "FI", "royaume uni": "GB", "grece": "GR", "hongrie": "HU",
    "indonesie": "ID", "irlande": "IE", "israel": "IL", "inde": "IN", "italie": "IT", "japon": "JP",
    "coree": "KR", "coree du sud": "KR", "lituanie": "LT", "lettonie": "LV", "maroc": "MA", "malte": "MT",
    "mexique": "MX", "malaisie": "MY", "pays bas": "NL", "norvege": "NO", "pologne": "PL", "roumanie": "RO",
    "serbie": "RS", "russie": "RU", "suede": "SE", "singapour": "SG", "slovenie": "SI", "slovaquie": "SK",
    "thailande": "TH", "tunisie": "TN", "turquie": "TR", "taiwan": "TW", "etats unis": "US",
    "afrique du sud": "ZA",
}


def _country_key(name: str) -> str:
    return re.sub(r"[^a-z]+", " ", strip_accents(name).lower()).strip()


_COUNTRY_LOOKUP = {**{_country_key(name): code for code, name in COUNTRIES.items()}, **_COUNTRY_ALIASES}


def normalize_country(value: str | None) -> str:
    """Map a country (ISO code, English or French name) to its ISO 3166-1 alpha-2 code. Empty = unknown."""
    v = (value or "").strip()
    if v.lower() in _EMPTY_VALUES:
        return ""
    if re.fullmatch(r"[A-Za-z]{2}", v):
        return v.upper()
    key = _country_key(v)
    if key in _COUNTRY_LOOKUP:
        return _COUNTRY_LOOKUP[key]
    raise ValidationError(f"Unrecognised country of origin: {value!r} (use the 2-letter ISO code, e.g. CN, MY, DE)")


def country_name(code: str) -> str:
    return COUNTRIES.get(code, code)


def connect(path: Path | str | None = None) -> sqlite3.Connection:
    p = Path(path or DB_PATH)
    p.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(p, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    _migrate(conn)
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


# Accepted spellings for each status, in English and French (BOMs and imports come from many tools).
STATUS_SYNONYMS = {
    "compliant": ("yes", "y", "ok", "compliant", "c", "1", "true", "rohs", "reach", "oui", "conforme"),
    "exemption": ("exemption", "exempt", "compliant_with_exemption", "conforme_avec_exemption", "exempte"),
    "svhc": ("svhc", "contains_svhc", "contient_svhc", "svhc>0.1%", "art33", "article_33"),
    "non_compliant": ("no", "n", "non_compliant", "noncompliant", "not_compliant", "0", "false", "nok",
                      "non", "non_conforme", "nonconforme"),
}


def clean_status(value: str | None, allowed: tuple[str, ...], field: str) -> str:
    """Accept varied values (Yes/Compliant/No/Oui/Non...) and map them to the enumeration."""
    v = strip_accents((value or "").strip().lower()).replace("-", "_").replace(" ", "_")
    if v in ("", "?", "n/a", "na", "unknown", "inconnu", "nc", "tbd", "needs_review", "a_verifier"):
        return "unknown"
    for status, words in STATUS_SYNONYMS.items():
        if v in words and status in allowed:
            return status
    status = _status_from_words(v, allowed)
    if status:
        return status
    raise ValidationError(f"Invalid status for {field!r}: {value!r} (expected: {', '.join(allowed)})")


# Words of the longer statuses written by distributors and PLM tools: "Compliant(YES)_", "ROHS3 Compliant",
# "RoHS Compliant by Exemption", "Not RoHS Compliant", "REACH Unaffected", "Contains SVHC", "SVHC free"...
_UNKNOWN_WORDS = {"unknown", "inconnu", "undefined", "tbd", "applicable", "available", "pending"}
_NEGATIVE_WORDS = {"no", "non", "not", "nok", "noncompliant", "nonconforme", "false", "incompliant"}
_POSITIVE_WORDS = {"yes", "y", "ok", "oui", "true", "compliant", "compliance", "conforme", "conformite", "unaffected"}
_NO_SVHC_WORDS = {"no", "non", "not", "free", "without", "sans", "aucune", "none"}
_SVHC_WORDS = {"svhc", "affected", "contains", "contient"}


def _status_from_words(v: str, allowed: tuple[str, ...]) -> str | None:
    """Status read from the words of a longer value, or None if it cannot be decided."""
    words = re.findall(r"[a-z]+|\d+", v)
    if not words:
        return None
    present = set(words)
    if present & _UNKNOWN_WORDS:
        return "unknown"  # "Unknown", "Vendor undefined", "Not applicable", "Not available"...
    if "svhc" in allowed and present & _SVHC_WORDS:
        # "Contains SVHC", "SVHC: Yes", "REACH Affected" / "SVHC free", "No SVHC", "SVHC: No"
        return "compliant" if present & _NO_SVHC_WORDS else "svhc"
    # "Compliant by exemption", "Yes with exemption"; but "no exemption" / "without exemption" is not a negation
    # of the compliance
    exempt = [i for i, w in enumerate(words) if w.startswith("exempt")]
    no_exemption = {i - 1 for i in exempt if i > 0 and words[i - 1] in ("no", "without", "sans", "aucune")}
    if "exemption" in allowed and any(i - 1 not in no_exemption for i in exempt):
        return "exemption"
    if any(w in _NEGATIVE_WORDS for i, w in enumerate(words) if i not in no_exemption):
        return "non_compliant" if "non_compliant" in allowed else None
    if present & _POSITIVE_WORDS:
        return "compliant"
    return None


# Dates are stored as YYYY-MM-DD (sortable, comparable) and shown as DD-MM-YYYY.
_DATE_FORMATS = ("%Y-%m-%d", "%d-%m-%Y", "%d/%m/%Y", "%d.%m.%Y", "%Y/%m/%d", "%d-%m-%y", "%d/%m/%y")


def normalize_date(value: str | None) -> str:
    """YYYY-MM-DD from DD-MM-YYYY, DD/MM/YYYY, DD.MM.YYYY or YYYY-MM-DD (a time after it is dropped).
    Unrecognised text is kept as written."""
    v = (value or "").strip()
    day = v[:10] if re.match(r"\d{4}-\d{2}-\d{2}[ T]", v) else v  # "2026-03-10 00:00:00" from a workbook
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(day, fmt).date().isoformat()
        except ValueError:
            continue
    return v


def format_date(value: str | None) -> str:
    """DD-MM-YYYY for display (from YYYY-MM-DD or a timestamp); other text unchanged."""
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", value or "")
    return f"{m[3]}-{m[2]}-{m[1]}" if m else (value or "")


def format_datetime(value: str | None) -> str:
    """DD-MM-YYYY HH:MM UTC for a stored timestamp."""
    try:
        t = datetime.fromisoformat(value or "")
    except ValueError:
        return value or ""
    if t.tzinfo:
        t = t.astimezone(timezone.utc)
    return t.strftime("%d-%m-%Y %H:%M") + (" UTC" if t.tzinfo else "")


def prepare(data: dict) -> dict:
    """Validate and normalise a component record."""
    mpn = (data.get("mpn") or "").strip()
    if not mpn:
        raise ValidationError("The manufacturer part number (mpn) is required")
    rec = {f: str(data.get(f) or "").strip() for f in FIELDS}
    rec["mpn"] = mpn
    rec["rohs2"] = clean_status(rec["rohs2"], ROHS_STATUSES, "rohs2")
    rec["rohs3"] = clean_status(rec["rohs3"], ROHS_STATUSES, "rohs3")
    rec["reach"] = clean_status(rec["reach"], REACH_STATUSES, "reach")
    rec["eccn"] = normalize_eccn(rec["eccn"])
    rec["country_of_origin"] = normalize_country(rec["country_of_origin"])
    rec["last_checked"] = normalize_date(rec["last_checked"])
    rec["mpn_norm"] = normalize_mpn(mpn)
    rec["mpn_loose"] = loose_mpn(mpn)
    rec["manufacturer_norm"] = normalize_manufacturer(rec["manufacturer"])
    rec["updated_at"] = _now()
    return rec


def manufacturer_links(rows) -> list[dict]:
    """Manufacturers of these components, with the key of their page and their number of components (most first)."""
    counts = Counter((r["manufacturer_norm"], r["manufacturer"]) for r in rows)
    return [{"key": key, "name": name, "count": n} for (key, name), n in counts.most_common()]


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if "manufacturer_norm" in d:
        d["manufacturer_key"] = d["manufacturer_norm"]  # link to the manufacturer record
    for k in ("mpn_norm", "mpn_loose", "manufacturer_norm"):
        d.pop(k, None)
    return d


def upsert(conn: sqlite3.Connection, data: dict) -> tuple[int, bool]:
    """Insert or update (key: MPN + manufacturer). Returns (id, created)."""
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
        raise ValidationError("This component (part number + manufacturer) already exists") from exc
    return cur.lastrowid


def update(conn: sqlite3.Connection, comp_id: int, data: dict) -> bool:
    rec = prepare(data)
    sets = ", ".join(f"{c} = ?" for c in rec)
    try:
        cur = conn.execute(f"UPDATE components SET {sets} WHERE id = ?", [*rec.values(), comp_id])
    except sqlite3.IntegrityError as exc:
        raise ValidationError("Another component already has this part number + manufacturer") from exc
    return cur.rowcount > 0


def delete(conn: sqlite3.Connection, comp_id: int) -> bool:
    conn.execute("DELETE FROM component_equivalents WHERE a = ? OR b = ?", (comp_id, comp_id))
    return conn.execute("DELETE FROM components WHERE id = ?", (comp_id,)).rowcount > 0


def get(conn: sqlite3.Connection, comp_id: int) -> dict | None:
    return row_to_dict(conn.execute("SELECT * FROM components WHERE id = ?", (comp_id,)).fetchone())


def search(conn: sqlite3.Connection, q: str = "", limit: int = 50, offset: int = 0, status: str = "") -> tuple[list[dict], int]:
    where, params = [], []
    if q:
        like = f"%{q.strip()}%"
        where.append("(mpn LIKE ? OR manufacturer LIKE ? OR description LIKE ? OR mpn_loose LIKE ?)")
        params += [like, like, like, f"%{loose_mpn(q)}%"]
    if status == "issues":
        where.append("(rohs2 IN ('non_compliant','unknown') OR rohs3 IN ('non_compliant','unknown') "
                     "OR reach IN ('non_compliant','unknown','svhc'))")
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
