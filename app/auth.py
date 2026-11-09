"""User accounts and login sessions.

- Passwords hashed with scrypt (standard library), with a random salt per account.
- Session: random token sent in an HttpOnly cookie; only its SHA-256 hash is stored in the
  database, so a database leak does not allow anyone to hijack a session.
"""

from __future__ import annotations

import hashlib
import hmac
import re
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from . import db

SESSION_COOKIE = "rrc_session"
SESSION_DAYS = 30
MIN_PASSWORD = 8

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class AuthError(Exception):
    """Authentication error (message meant for the user)."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def hash_password(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode("utf-8"), salt=salt, dklen=32, **_SCRYPT)
    return f"scrypt${_SCRYPT['n']}${_SCRYPT['r']}${_SCRYPT['p']}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        algo, n, r, p, salt, digest = stored.split("$")
        if algo != "scrypt":
            return False
        test = hashlib.scrypt(password.encode("utf-8"), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                              dklen=len(bytes.fromhex(digest)))
        return hmac.compare_digest(test, bytes.fromhex(digest))
    except (ValueError, TypeError):
        return False


# Dummy hash: a password is still checked when the e-mail is unknown,
# so that the response time does not reveal which accounts exist.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _public(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "email": row["email"], "name": row["name"] or row["email"].split("@")[0], "created_at": row["created_at"]}


def create_user(conn: sqlite3.Connection, email: str, password: str, name: str = "") -> dict:
    email = normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise db.ValidationError("Invalid e-mail address")
    if len(password or "") < MIN_PASSWORD:
        raise db.ValidationError(f"The password must be at least {MIN_PASSWORD} characters long")
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        raise AuthError("An account already exists with this e-mail address")
    cur = conn.execute(
        "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (email, (name or "").strip()[:80], hash_password(password), _now().isoformat()),
    )
    return _public(conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())


def authenticate(conn: sqlite3.Connection, email: str, password: str) -> dict:
    row = conn.execute("SELECT * FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
    ok = verify_password(password or "", row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok:
        raise AuthError("Incorrect e-mail or password")
    return _public(row)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))  # clean-up
    conn.execute(
        "INSERT INTO sessions (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (_token_hash(token), user_id, now.isoformat(), (now + timedelta(days=SESSION_DAYS)).isoformat()),
    )
    return token


def user_from_token(conn: sqlite3.Connection, token: str | None) -> dict | None:
    if not token:
        return None
    row = conn.execute(
        "SELECT u.* FROM sessions s JOIN users u ON u.id = s.user_id WHERE s.token_hash = ? AND s.expires_at > ?",
        (_token_hash(token), _now().isoformat()),
    ).fetchone()
    return _public(row) if row else None


def delete_session(conn: sqlite3.Connection, token: str | None) -> None:
    if token:
        conn.execute("DELETE FROM sessions WHERE token_hash = ?", (_token_hash(token),))
