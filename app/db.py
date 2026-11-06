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

Export / douane :
  - eccn              : Export Control Classification Number (EAR américain / liste UE des biens à
                        double usage), ex. "EAR99", "5A992.c", "3A991.a.1" ; vide = inconnu
  - country_of_origin : pays d'origine, code ISO 3166-1 alpha-2 (ex. "CN", "MY", "DE") ; vide = inconnu
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
    rohs2             TEXT NOT NULL DEFAULT 'inconnu',
    rohs3             TEXT NOT NULL DEFAULT 'inconnu',
    reach             TEXT NOT NULL DEFAULT 'inconnu',
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

-- Équivalences confirmées par un utilisateur (stockées dans les deux sens)
CREATE TABLE IF NOT EXISTS component_equivalents (
    a INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    b INTEGER NOT NULL REFERENCES components(id) ON DELETE CASCADE,
    PRIMARY KEY (a, b)
);

-- Comptes utilisateurs (mot de passe haché avec scrypt, jamais stocké en clair)
CREATE TABLE IF NOT EXISTS users (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    email         TEXT NOT NULL UNIQUE,
    name          TEXT NOT NULL DEFAULT '',
    password_hash TEXT NOT NULL,
    created_at    TEXT NOT NULL
);

-- Sessions de connexion : seule l'empreinte SHA-256 du jeton est conservée
CREATE TABLE IF NOT EXISTS sessions (
    token_hash TEXT PRIMARY KEY,
    user_id    INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at TEXT NOT NULL,
    expires_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions (user_id);

-- Historique des BOM analysées par utilisateur (fichier d'origine conservé pour pouvoir la rouvrir)
CREATE TABLE IF NOT EXISTS bom_history (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    filename      TEXT NOT NULL,
    file_data     BLOB NOT NULL,
    mapping       TEXT NOT NULL DEFAULT '',
    substitutions TEXT NOT NULL DEFAULT '',
    summary       TEXT NOT NULL DEFAULT '',
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_bom_history_user ON bom_history (user_id, updated_at);

-- Codes ECCN : intitulé et notes saisis par l'utilisateur (l'explication est calculée)
CREATE TABLE IF NOT EXISTS eccn_codes (
    code       TEXT PRIMARY KEY,
    title      TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);

-- Informations sur les fabricants (clé = nom normalisé, comme components.manufacturer_norm)
CREATE TABLE IF NOT EXISTS manufacturers (
    key        TEXT PRIMARY KEY,
    name       TEXT NOT NULL DEFAULT '',
    website    TEXT NOT NULL DEFAULT '',
    country    TEXT NOT NULL DEFAULT '',
    notes      TEXT NOT NULL DEFAULT '',
    updated_at TEXT NOT NULL
);
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


# Colonnes ajoutées après la première version : ajoutées aux bases existantes à l'ouverture.
_ADDED_COLUMNS = {
    "eccn": "TEXT NOT NULL DEFAULT ''",
    "country_of_origin": "TEXT NOT NULL DEFAULT ''",
}


def _migrate(conn: sqlite3.Connection) -> None:
    existing = {r[1] for r in conn.execute("PRAGMA table_info(components)")}
    for col, decl in _ADDED_COLUMNS.items():
        if col not in existing:
            conn.execute(f"ALTER TABLE components ADD COLUMN {col} {decl}")
    conn.commit()


# ------------------------------------------------------------------ ECCN et pays d'origine

_ECCN_RE = re.compile(r"^(\d)([A-E])(\d{3})((?:\.[A-Z0-9]+)*)$", re.I)


def normalize_eccn(value: str | None) -> str:
    """Normalise un ECCN : "5a992.C" -> "5A992.c", "ear 99" -> "EAR99". Vide = inconnu."""
    v = re.sub(r"\s+", "", (value or "")).strip().rstrip(".")
    if v.lower() in ("", "?", "na", "n/a", "inconnu", "unknown", "tbd", "nc"):
        return ""
    if v.upper() == "EAR99":
        return "EAR99"
    m = _ECCN_RE.match(v)
    if not m:
        raise ValidationError(f"ECCN invalide : {value!r} (attendu : EAR99 ou un code comme 5A992.c, 3A001.a.1)")
    return f"{m.group(1)}{m.group(2).upper()}{m.group(3)}{m.group(4).lower()}"


def eccn_is_controlled(eccn: str) -> bool:
    """Vrai pour un ECCN de la liste de contrôle (tout sauf EAR99 et vide)."""
    return bool(eccn) and eccn != "EAR99"


# Code ISO -> nom français, puis alias (anglais, variantes) -> code
COUNTRIES = {
    "AT": "Autriche", "AU": "Australie", "BE": "Belgique", "BG": "Bulgarie", "BR": "Brésil", "CA": "Canada",
    "CH": "Suisse", "CN": "Chine", "CR": "Costa Rica", "CZ": "Tchéquie", "DE": "Allemagne", "DK": "Danemark",
    "EE": "Estonie", "ES": "Espagne", "FI": "Finlande", "FR": "France", "GB": "Royaume-Uni", "GR": "Grèce",
    "HK": "Hong Kong", "HU": "Hongrie", "ID": "Indonésie", "IE": "Irlande", "IL": "Israël", "IN": "Inde",
    "IT": "Italie", "JP": "Japon", "KR": "Corée du Sud", "LT": "Lituanie", "LU": "Luxembourg", "LV": "Lettonie",
    "MA": "Maroc", "MT": "Malte", "MX": "Mexique", "MY": "Malaisie", "NL": "Pays-Bas", "NO": "Norvège",
    "PH": "Philippines", "PL": "Pologne", "PT": "Portugal", "RO": "Roumanie", "RS": "Serbie", "RU": "Russie",
    "SE": "Suède", "SG": "Singapour", "SI": "Slovénie", "SK": "Slovaquie", "TH": "Thaïlande", "TN": "Tunisie",
    "TR": "Turquie", "TW": "Taïwan", "UA": "Ukraine", "US": "États-Unis", "VN": "Viêt Nam", "ZA": "Afrique du Sud",
}
_COUNTRY_ALIASES = {
    "austria": "AT", "australia": "AU", "belgium": "BE", "bulgaria": "BG", "brazil": "BR", "switzerland": "CH",
    "china": "CN", "prc": "CN", "people s republic of china": "CN", "czech republic": "CZ", "czechia": "CZ",
    "republique tcheque": "CZ", "germany": "DE", "denmark": "DK", "estonia": "EE", "spain": "ES", "finland": "FI",
    "united kingdom": "GB", "uk": "GB", "great britain": "GB", "england": "GB", "greece": "GR", "hungary": "HU",
    "indonesia": "ID", "ireland": "IE", "israel": "IL", "india": "IN", "italy": "IT", "japan": "JP",
    "korea": "KR", "south korea": "KR", "republic of korea": "KR", "coree": "KR", "lithuania": "LT", "latvia": "LV",
    "morocco": "MA", "mexico": "MX", "malaysia": "MY", "netherlands": "NL", "the netherlands": "NL",
    "holland": "NL", "norway": "NO", "philippines": "PH", "poland": "PL", "romania": "RO", "serbia": "RS",
    "russia": "RU", "russian federation": "RU", "sweden": "SE", "singapore": "SG", "slovenia": "SI",
    "slovakia": "SK", "thailand": "TH", "tunisia": "TN", "turkey": "TR", "turkiye": "TR", "taiwan": "TW",
    "republic of china": "TW", "chinese taipei": "TW", "ukraine": "UA", "usa": "US", "united states": "US",
    "united states of america": "US", "etats unis": "US", "vietnam": "VN", "viet nam": "VN", "south africa": "ZA",
    "uk great britain": "GB",
}
_COUNTRY_LOOKUP = {**{norm: code for code, name in COUNTRIES.items() for norm in [re.sub(r"[^a-z]+", " ", strip_accents(name).lower()).strip()]}, **_COUNTRY_ALIASES}


def normalize_country(value: str | None) -> str:
    """Ramène un pays (code ISO, nom français ou anglais) à son code ISO 3166-1 alpha-2. Vide = inconnu."""
    v = (value or "").strip()
    if v.lower() in ("", "?", "na", "n/a", "inconnu", "unknown", "tbd", "nc"):
        return ""
    if re.fullmatch(r"[A-Za-z]{2}", v):
        return v.upper()
    key = re.sub(r"[^a-z]+", " ", strip_accents(v).lower()).strip()
    if key in _COUNTRY_LOOKUP:
        return _COUNTRY_LOOKUP[key]
    raise ValidationError(f"Pays d'origine non reconnu : {value!r} (utilisez le code ISO à 2 lettres, ex. CN, MY, DE)")


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
    rec["eccn"] = normalize_eccn(rec["eccn"])
    rec["country_of_origin"] = normalize_country(rec["country_of_origin"])
    rec["mpn_norm"] = normalize_mpn(mpn)
    rec["mpn_loose"] = loose_mpn(mpn)
    rec["manufacturer_norm"] = normalize_manufacturer(rec["manufacturer"])
    rec["updated_at"] = _now()
    return rec


def row_to_dict(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    d = dict(row)
    if "manufacturer_norm" in d:
        d["manufacturer_key"] = d["manufacturer_norm"]  # lien vers la fiche fabricant
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
