"""Account-page sessions (DESIGN.md, The account page > Sessions and form protections).

A session starts after an account-page Discord login and lasts 30 minutes from
sign-in (the expiry doesn't slide). The row holds the SHA-256 of the session id,
the Discord user id, and a display username. No Discord token is kept.

The CSRF token is HMAC-SHA256 keyed with the session id over a fixed label: it
is stable for the session, reveals nothing about the id, and is never stored.
Every POST recomputes it from the cookie and compares in constant time.

A session never outlives the membership: registry.revoke_in_tx deletes the
user's sessions in the same transaction, whatever path the member leaves by.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
from dataclasses import dataclass

# __Host-: Secure, Path=/, and no Domain, so a sibling subdomain can't plant a
# session of its own (session fixation).
SESSION_COOKIE = "__Host-os_session"
SESSION_TTL_SECONDS = 1800
CHANGE_WINDOW_SECONDS = 3600
MAX_CHANGES_PER_WINDOW = 10
MAX_USERNAME_CHARS = 100
_CSRF_LABEL = b"outer-steppes account csrf"


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def csrf_for(session_id: str) -> str:
    return hmac.new(session_id.encode("utf-8"), _CSRF_LABEL, hashlib.sha256).hexdigest()


@dataclass(frozen=True)
class Session:
    session_id: str          # the cookie value; never stored
    discord_user_id: str
    username: str
    expires_at: int

    @property
    def csrf(self) -> str:
        return csrf_for(self.session_id)

    def csrf_matches(self, presented: str | None) -> bool:
        return bool(presented) and hmac.compare_digest(presented.encode("utf-8"), self.csrf.encode("utf-8"))


def create_in_tx(conn: sqlite3.Connection, discord_user_id: str, username: str, now: int) -> str:
    """Inside the caller's transaction. Returns the session id (the cookie value), shown once."""
    session_id = secrets.token_urlsafe(32)
    conn.execute("INSERT INTO web_sessions (session_hash, discord_user_id, username, expires_at) VALUES (?, ?, ?, ?)",
                 (_hash(session_id), discord_user_id, username[:MAX_USERNAME_CHARS], now + SESSION_TTL_SECONDS))
    return session_id


def load(conn: sqlite3.Connection, session_id: str | None, now: int) -> Session | None:
    if not session_id:
        return None
    row = conn.execute("SELECT * FROM web_sessions WHERE session_hash = ? AND expires_at > ?",
                       (_hash(session_id), now)).fetchone()
    if row is None:
        return None
    return Session(session_id, row["discord_user_id"], row["username"], row["expires_at"])


def delete_in_tx(conn: sqlite3.Connection, session: Session) -> None:
    conn.execute("DELETE FROM web_sessions WHERE session_hash = ?", (_hash(session.session_id),))


def delete_for_user_in_tx(conn: sqlite3.Connection, discord_user_id: str) -> int:
    return conn.execute("DELETE FROM web_sessions WHERE discord_user_id = ?", (discord_user_id,)).rowcount


def changes_in_window(conn: sqlite3.Connection, discord_user_id: str, now: int) -> int:
    return conn.execute("SELECT COUNT(*) FROM account_changes WHERE discord_user_id = ? AND at > ?",
                        (discord_user_id, now - CHANGE_WINDOW_SECONDS)).fetchone()[0]


def record_change_in_tx(conn: sqlite3.Connection, discord_user_id: str, now: int) -> None:
    conn.execute("INSERT INTO account_changes (discord_user_id, at) VALUES (?, ?)", (discord_user_id, now))


def delete_expired_in_tx(conn: sqlite3.Connection, now: int) -> dict[str, int]:
    return {
        "web_sessions": conn.execute("DELETE FROM web_sessions WHERE expires_at <= ?", (now,)).rowcount,
        "account_changes": conn.execute("DELETE FROM account_changes WHERE at <= ?",
                                        (now - CHANGE_WINDOW_SECONDS,)).rowcount,
    }
