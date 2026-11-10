"""Who may change the component database.

- Open editing (RRC_OPEN_EDITING=1, for development or a private server): anyone, without an account.
- Otherwise: the administrator (the account whose address is RRC_ADMIN_EMAIL), or a signed-in account the
  administrator approved. A user asks for edit access from the Database tab; the administrator receives an
  e-mail with a link to approve or refuse the request.
- Approved users can add RRC_DAILY_ADDITIONS new components per 24 hours (default 20). The administrator has
  no limit and is the only one who can delete components.

Browsing and BOM analysis stay open to everyone.
"""

from __future__ import annotations

import hashlib
import os
import secrets
import sqlite3
from datetime import datetime, timedelta, timezone

from . import auth

REVIEW_DAYS = 7          # validity of the administrator's review link
REQUEST_EVERY_HOURS = 24  # a user can send one request per day
DEFAULT_DAILY_ADDITIONS = 20


class EditAccessError(Exception):
    """Edit access request refused (message meant for the user)."""


class LimitReached(Exception):
    """Daily limit of added components reached (message meant for the user)."""


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("ascii")).hexdigest()


def open_editing() -> bool:
    return os.environ.get("RRC_OPEN_EDITING", "").lower() in ("1", "true", "yes", "on")


def admin_email() -> str:
    return auth.normalize_email(os.environ.get("RRC_ADMIN_EMAIL", ""))


def daily_limit() -> int:
    try:
        return max(0, int(os.environ.get("RRC_DAILY_ADDITIONS", DEFAULT_DAILY_ADDITIONS)))
    except ValueError:
        return DEFAULT_DAILY_ADDITIONS


def is_admin(user: dict | None) -> bool:
    return bool(user and admin_email() and user["email"] == admin_email())


def can_edit(user: dict | None) -> bool:
    return open_editing() or is_admin(user) or bool(user and user.get("edit_access") == "approved")


# ---------------------------------------------------------------- daily limit

def additions_left(conn: sqlite3.Connection, user: dict | None) -> int | None:
    """Components the user can still add in the last 24 hours; None = no limit."""
    if open_editing() or not user or is_admin(user):
        return None
    since = (_now() - timedelta(hours=24)).isoformat()
    used = conn.execute("SELECT COALESCE(SUM(count), 0) FROM additions WHERE user_id = ? AND created_at > ?",
                        (user["id"], since)).fetchone()[0]
    return max(0, daily_limit() - used)


def check_additions(conn: sqlite3.Connection, user: dict | None, count: int) -> None:
    left = additions_left(conn, user)
    if left is not None and count > left:
        limit = daily_limit()
        if count == 1:
            raise LimitReached(f"Daily limit reached: {limit} new components per 24 hours. Try again tomorrow")
        raise LimitReached(f"This would add {count} components, but you can add {left} more today "
                           f"(limit: {limit} new components per 24 hours)")


def record_additions(conn: sqlite3.Connection, user: dict | None, count: int) -> None:
    if count > 0 and user and additions_left(conn, user) is not None:
        conn.execute("INSERT INTO additions (user_id, count, created_at) VALUES (?, ?, ?)",
                     (user["id"], count, _now().isoformat()))


# ---------------------------------------------------------------- edit access requests

def request_access(conn: sqlite3.Connection, user: dict) -> str:
    """Records the request and returns the token for the administrator's review link."""
    if can_edit(user):
        raise EditAccessError("Your account can already change the database")
    row = conn.execute("SELECT edit_access, edit_requested_at FROM users WHERE id = ?", (user["id"],)).fetchone()
    recent = row["edit_requested_at"] and row["edit_requested_at"] > (_now() - timedelta(hours=REQUEST_EVERY_HOURS)).isoformat()
    if recent and row["edit_access"] == "pending":
        raise EditAccessError("Your request was already sent: the administrator has not answered yet")
    if recent:
        raise EditAccessError("You can send a new request tomorrow")
    token = secrets.token_urlsafe(32)
    conn.execute("UPDATE users SET edit_access = 'pending', edit_token_hash = ?, edit_requested_at = ?, "
                 "edit_reviewed_at = NULL WHERE id = ?", (_hash(token), _now().isoformat(), user["id"]))
    return token


def find_request(conn: sqlite3.Connection, token: str) -> sqlite3.Row | None:
    """Pending request matching a review link, or None (unknown, used or expired link)."""
    since = (_now() - timedelta(days=REVIEW_DAYS)).isoformat()
    return conn.execute(
        "SELECT * FROM users WHERE edit_token_hash = ? AND edit_access = 'pending' AND edit_requested_at > ?",
        (_hash(token or "x"), since),
    ).fetchone()


def review(conn: sqlite3.Connection, token: str, approve: bool) -> sqlite3.Row:
    row = find_request(conn, token)
    if not row:
        raise EditAccessError("This link is invalid, has expired or was already used")
    conn.execute("UPDATE users SET edit_access = ?, edit_token_hash = NULL, edit_reviewed_at = ? WHERE id = ?",
                 ("approved" if approve else "refused", _now().isoformat(), row["id"]))
    return conn.execute("SELECT * FROM users WHERE id = ?", (row["id"],)).fetchone()


def set_access(conn: sqlite3.Connection, email: str, approved: bool) -> bool:
    """Approves or revokes an account directly (command line). False if the account does not exist."""
    cur = conn.execute("UPDATE users SET edit_access = ?, edit_token_hash = NULL, edit_reviewed_at = ? WHERE email = ?",
                       ("approved" if approved else "", _now().isoformat(), auth.normalize_email(email)))
    return cur.rowcount > 0
