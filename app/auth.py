"""Comptes utilisateurs et sessions de connexion.

- Mots de passe hachés avec scrypt (bibliothèque standard), sel aléatoire par compte.
- Session : jeton aléatoire envoyé dans un cookie HttpOnly ; seule son empreinte SHA-256 est
  stockée en base, si bien qu'une fuite de la base ne permet pas d'usurper une session.
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
    """Erreur d'authentification (message destiné à l'utilisateur)."""


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


# Empreinte factice : on vérifie quand même un mot de passe si l'e-mail est inconnu,
# pour que le temps de réponse ne révèle pas quels comptes existent.
_DUMMY_HASH = hash_password(secrets.token_hex(16))


def normalize_email(email: str) -> str:
    return (email or "").strip().lower()


def _public(row: sqlite3.Row) -> dict:
    return {"id": row["id"], "email": row["email"], "name": row["name"] or row["email"].split("@")[0], "created_at": row["created_at"]}


def create_user(conn: sqlite3.Connection, email: str, password: str, name: str = "") -> dict:
    email = normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise db.ValidationError("Adresse e-mail invalide")
    if len(password or "") < MIN_PASSWORD:
        raise db.ValidationError(f"Le mot de passe doit contenir au moins {MIN_PASSWORD} caractères")
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone():
        raise AuthError("Un compte existe déjà avec cette adresse e-mail")
    cur = conn.execute(
        "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (email, (name or "").strip()[:80], hash_password(password), _now().isoformat()),
    )
    return _public(conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())


def authenticate(conn: sqlite3.Connection, email: str, password: str) -> dict:
    row = conn.execute("SELECT * FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
    ok = verify_password(password or "", row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok:
        raise AuthError("E-mail ou mot de passe incorrect")
    return _public(row)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def create_session(conn: sqlite3.Connection, user_id: int) -> str:
    token = secrets.token_urlsafe(32)
    now = _now()
    conn.execute("DELETE FROM sessions WHERE expires_at < ?", (now.isoformat(),))  # ménage
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
