"""The claim link: how the owner becomes a bridge's first member (DESIGN.md, Claiming the bridge; ADR-0016).

A bridge with no active members is unclaimed; nothing else records it. While it is,
the operator mints a link through the admin API. Only the secret's SHA-256 and its
expiry are stored, in `settings`. A new link replaces the last, and creating any
member deletes it. The pending Discord login keeps the hash of the link that started
it, so a link replaced mid-login fails at the callback.
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3

CLAIM_PATH = "/account/claim"
CLAIM_TTL_SECONDS = 3600


def secret_hash(secret: str) -> str:
    return hashlib.sha256(secret.encode("utf-8")).hexdigest()


def is_claimed(conn: sqlite3.Connection) -> bool:
    return conn.execute("SELECT 1 FROM registrations WHERE revoked_at IS NULL LIMIT 1").fetchone() is not None


def mint_in_tx(conn: sqlite3.Connection, now: int) -> tuple[str, int]:
    """A new claim secret, replacing any earlier one. Returned once; only its hash is stored."""
    secret = secrets.token_urlsafe(32)
    expires_at = now + CLAIM_TTL_SECONDS
    conn.executemany("INSERT INTO settings (key, value) VALUES (?, ?) "
                     "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
                     (("claim_hash", secret_hash(secret)), ("claim_expires_at", str(expires_at))))
    return secret, expires_at


def clear_in_tx(conn: sqlite3.Connection) -> None:
    conn.execute("DELETE FROM settings WHERE key IN ('claim_hash', 'claim_expires_at')")


def current_hash(conn: sqlite3.Connection, now: int) -> str | None:
    """The live claim secret's hash: unexpired, on an unclaimed bridge. None otherwise."""
    rows = dict(conn.execute("SELECT key, value FROM settings WHERE key IN ('claim_hash', 'claim_expires_at')"))
    expires_at = rows.get("claim_expires_at", "")
    if "claim_hash" not in rows or not expires_at.isdigit() or int(expires_at) <= now or is_claimed(conn):
        return None
    return rows["claim_hash"]


def hash_is_current(conn: sqlite3.Connection, presented_hash: str | None, now: int) -> bool:
    current = current_hash(conn, now)
    return current is not None and presented_hash is not None and hmac.compare_digest(presented_hash, current)
