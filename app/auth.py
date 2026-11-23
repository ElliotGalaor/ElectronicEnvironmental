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
    return {"id": row["id"], "email": row["email"], "name": row["name"] or row["email"].split("@")[0],
            "created_at": row["created_at"], "edit_access": row["edit_access"]}


def _check_fields(email: str, password: str) -> str:
    email = normalize_email(email)
    if not _EMAIL_RE.match(email):
        raise db.ValidationError("Invalid e-mail address")
    if len(password or "") < MIN_PASSWORD:
        raise db.ValidationError(f"The password must be at least {MIN_PASSWORD} characters long")
    return email


def _account_exists(conn: sqlite3.Connection, email: str) -> bool:
    return conn.execute("SELECT 1 FROM users WHERE email = ?", (email,)).fetchone() is not None


def _check_new_account(conn: sqlite3.Connection, email: str, password: str) -> str:
    email = _check_fields(email, password)
    if _account_exists(conn, email):
        raise AuthError("An account already exists with this e-mail address")
    return email


def _insert_user(conn: sqlite3.Connection, email: str, name: str, password_hash: str) -> dict:
    cur = conn.execute(
        "INSERT INTO users (email, name, password_hash, created_at) VALUES (?, ?, ?, ?)",
        (email, (name or "").strip()[:80], password_hash, _now().isoformat()),
    )
    return _public(conn.execute("SELECT * FROM users WHERE id = ?", (cur.lastrowid,)).fetchone())


def create_user(conn: sqlite3.Connection, email: str, password: str, name: str = "") -> dict:
    email = _check_new_account(conn, email, password)
    return _insert_user(conn, email, name, hash_password(password))


# ---------------------------------------------------------------- sign-up confirmed by e-mail
#
# Each attempt keeps the password typed in that attempt, and the link only works with that password.
# Someone who signs up first with another person's address therefore cannot take over the account:
# the owner of the mailbox confirms with their own password, and only their own attempt matches.

SIGNUP_HOURS = 24
SIGNUP_MAX_PER_EMAIL = 3   # pending attempts per address, to limit e-mails sent to someone else's mailbox


class TooManyRequests(AuthError):
    """Too many confirmation e-mails for one address."""


def create_signup_request(conn: sqlite3.Connection, email: str, password: str, name: str = "") -> tuple[str, bool]:
    """Records a sign-up attempt. Returns the token for the confirmation link, and whether the address already
    has an account.

    The visitor gets the same answer either way, so that sign-up does not reveal which addresses have an
    account: the owner of an existing account is told by e-mail instead of receiving a link. Such an attempt is
    recorded too (its token is never sent), so the limit per address answers the same way for both."""
    email = _check_fields(email, password)
    exists = _account_exists(conn, email)
    now = _now()
    conn.execute("DELETE FROM signup_requests WHERE expires_at < ?", (now.isoformat(),))  # clean-up
    pending = conn.execute("SELECT COUNT(*) FROM signup_requests WHERE email = ?", (email,)).fetchone()[0]
    if pending >= SIGNUP_MAX_PER_EMAIL:
        raise TooManyRequests("Several confirmation e-mails were already sent to this address: "
                              "check your mailbox (and spam folder) or try again tomorrow")
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO signup_requests (token_hash, email, name, password_hash, created_at, expires_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (_token_hash(token), email, (name or "").strip()[:80], hash_password(password), now.isoformat(),
         (now + timedelta(hours=SIGNUP_HOURS)).isoformat()),
    )
    return token, exists


def confirm_signup(conn: sqlite3.Connection, token: str, password: str) -> dict:
    """Creates the account of a confirmed sign-up. The password must be the one chosen at sign-up."""
    row = conn.execute(
        "SELECT * FROM signup_requests WHERE token_hash = ? AND expires_at > ?",
        (_token_hash(token or "x"), _now().isoformat()),
    ).fetchone()
    if not row:
        raise AuthError("This confirmation link is invalid or has expired: create the account again")
    if not verify_password(password or "", row["password_hash"]):
        raise AuthError("This is not the password chosen when creating the account")
    conn.execute("DELETE FROM signup_requests WHERE email = ?", (row["email"],))
    if conn.execute("SELECT 1 FROM users WHERE email = ?", (row["email"],)).fetchone():
        raise AuthError("This address is already confirmed: sign in")
    return _insert_user(conn, row["email"], row["name"], row["password_hash"])


def signup_pending(conn: sqlite3.Connection, email: str, password: str) -> bool:
    """True if a sign-up with this address and password is waiting for confirmation."""
    rows = conn.execute(
        "SELECT password_hash FROM signup_requests WHERE email = ? AND expires_at > ?",
        (normalize_email(email), _now().isoformat()),
    ).fetchall()
    return any(verify_password(password or "", r["password_hash"]) for r in rows)


# ---------------------------------------------------------------- forgotten password
#
# A single-use link valid for an hour, e-mailed to the address of the account. Only the hash of its token is
# stored, like the sessions. Choosing a new password signs the account out everywhere.

RESET_MINUTES = 60
RESET_MAX_PER_ACCOUNT = 3   # links valid at the same time, to limit the e-mails sent to one mailbox


def create_password_reset(conn: sqlite3.Connection, email: str) -> str | None:
    """Token of a reset link for the account of this address, or None: no account, or already several links
    waiting. The visitor gets the same answer either way, so that the form does not reveal which addresses
    have an account."""
    now = _now()
    conn.execute("DELETE FROM password_resets WHERE expires_at < ?", (now.isoformat(),))  # clean-up
    row = conn.execute("SELECT id FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
    if not row:
        return None
    waiting = conn.execute("SELECT COUNT(*) FROM password_resets WHERE user_id = ?", (row["id"],)).fetchone()[0]
    if waiting >= RESET_MAX_PER_ACCOUNT:
        return None
    token = secrets.token_urlsafe(32)
    conn.execute(
        "INSERT INTO password_resets (token_hash, user_id, created_at, expires_at) VALUES (?, ?, ?, ?)",
        (_token_hash(token), row["id"], now.isoformat(), (now + timedelta(minutes=RESET_MINUTES)).isoformat()),
    )
    return token


def _set_password_hash(conn: sqlite3.Connection, user_id: int, password: str) -> None:
    if len(password or "") < MIN_PASSWORD:
        raise db.ValidationError(f"The password must be at least {MIN_PASSWORD} characters long")
    conn.execute("UPDATE users SET password_hash = ? WHERE id = ?", (hash_password(password), user_id))
    conn.execute("DELETE FROM password_resets WHERE user_id = ?", (user_id,))  # every other link stops working
    conn.execute("DELETE FROM sessions WHERE user_id = ?", (user_id,))  # signed out everywhere


def reset_password(conn: sqlite3.Connection, token: str, password: str) -> dict:
    """New password chosen with a reset link. Returns the account, to sign it in."""
    row = conn.execute(
        "SELECT u.* FROM password_resets r JOIN users u ON u.id = r.user_id WHERE r.token_hash = ? AND r.expires_at > ?",
        (_token_hash(token or "x"), _now().isoformat()),
    ).fetchone()
    if not row:
        raise AuthError("This link is invalid, has expired or was already used: ask for a new one")
    _set_password_hash(conn, row["id"], password)
    return _public(row)


def set_password(conn: sqlite3.Connection, email: str, password: str) -> bool:
    """New password set by the administrator (command line, for a server that sends no e-mail).
    False when the address has no account."""
    row = conn.execute("SELECT id FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
    if not row:
        return False
    _set_password_hash(conn, row["id"], password)
    return True


def authenticate(conn: sqlite3.Connection, email: str, password: str) -> dict:
    row = conn.execute("SELECT * FROM users WHERE email = ?", (normalize_email(email),)).fetchone()
    ok = verify_password(password or "", row["password_hash"] if row else _DUMMY_HASH)
    if not row or not ok:
        raise AuthError("Incorrect e-mail or password")
    return _public(row)


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()  # a garbled link is just an unknown token


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
