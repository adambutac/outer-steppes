"""Admission checks for the ACK transaction (DESIGN.md, ACK steps 9 and 11).

Every function here runs inside the ACK's BEGIN IMMEDIATE transaction and
returns the refusal copy, or None when the check passes.
"""

from __future__ import annotations

import sqlite3

from .config import Config

THREAD_CHANNEL_TYPES = (10, 11, 12)

MSG_GUILD_NOT_ENABLED = "This server isn't enabled for Grok."
MSG_NOT_ALLOWED_HERE = "Not allowed here."
MSG_PAUSED = "Grok is paused."
MSG_SLOW_DOWN = "Slow down."
MSG_ALREADY_WORKING = "Already working on your last message here."
MSG_TOO_MANY_HERE = "Too many of your messages are still running here. Wait for one to finish."
MSG_BUSY = "Busy, try again."


def parent_channel_id(channel: dict, channel_id: str | None) -> str | None:
    """The channel an allowlist entry names: a thread's parent, else the channel."""
    if channel.get("type") in THREAD_CHANNEL_TYPES and channel.get("parent_id"):
        return str(channel["parent_id"])
    return channel_id


def allowlist_refusal(config: Config, guild_id: str | None, guild_installed: bool,
                      parent_channel: str | None, caller_id: str) -> str | None:
    """DESIGN.md, Install contexts.

    GUILD_ALLOWLIST and CHANNEL_ALLOWLIST restrict servers that installed the
    app; they do not apply to a member's own (user) install elsewhere, where
    their registration is the gate. USER_ALLOWLIST applies everywhere.
    """
    if guild_id is not None and guild_installed:
        if config.guild_allowlist and guild_id not in config.guild_allowlist:
            return MSG_GUILD_NOT_ENABLED
        if config.channel_allowlist and parent_channel not in config.channel_allowlist:
            return MSG_NOT_ALLOWED_HERE
    if config.user_allowlist and caller_id not in config.user_allowlist:
        return MSG_NOT_ALLOWED_HERE
    return None


def is_paused(conn: sqlite3.Connection) -> bool:
    row = conn.execute("SELECT value FROM settings WHERE key = 'paused'").fetchone()
    return row is not None and row["value"] == "1"


def caller_rate_refusal(conn: sqlite3.Connection, caller_id: str, now: int,
                        per_minute: int, per_day: int) -> str | None:
    """Trailing 60 s and 86,400 s windows over accepted /ask calls."""
    minute, day = conn.execute(
        "SELECT "
        "  coalesce(sum(at > ?), 0), "
        "  coalesce(sum(at > ?), 0) "
        "FROM rate_events WHERE subject_kind = 'caller' AND subject_id = ?",
        (now - 60, now - 86_400, caller_id),
    ).fetchone()
    if minute >= per_minute or day >= per_day:
        return MSG_SLOW_DOWN
    return None


MSG_OUTSTANDING = "This automation has too many unanswered requests. Try again later."


def owner_rate_refusal(conn: sqlite3.Connection, owner_id: str, now: int,
                       per_minute: int, per_day: int) -> str | None:
    """The owner's bucket: the calls routed to their automation, which are their own (ADR-0009)."""
    minute, day = conn.execute(
        "SELECT coalesce(sum(at > ?), 0), coalesce(sum(at > ?), 0) "
        "FROM rate_events WHERE subject_kind = 'owner' AND subject_id = ?",
        (now - 60, now - 86_400, owner_id),
    ).fetchone()
    if minute >= per_minute or day >= per_day:
        return MSG_SLOW_DOWN
    return None


def outstanding_refusal(conn: sqlite3.Connection, registration_id: str, max_outstanding: int) -> str | None:
    """Caps unanswered requests per automation, so a broken one wastes few runs."""
    count = conn.execute(
        "SELECT count(*) FROM interactions WHERE registration_id = ? "
        "AND status IN ('queued', 'posted', 'answering')",
        (registration_id,),
    ).fetchone()[0]
    return MSG_OUTSTANDING if count >= max_outstanding else None


def per_channel_refusal(conn: sqlite3.Connection, guild_id: str | None, channel_id: str | None,
                        caller_id: str, mac: str, now: int, max_asks: int,
                        posted_window_seconds: int) -> str | None:
    """The caller's live asks in this channel: queued or answering, or posted for the first window seconds.

    A live ask with the same prompt_mac is refused first, so a double-submit
    or quick retry doesn't run twice; otherwise at most max_asks run at once.
    """
    live = conn.execute(
        "SELECT prompt_mac FROM interactions "
        "WHERE guild_id IS ? AND channel_id IS ? AND caller_id = ? AND ("
        "  status IN ('queued', 'answering') "
        "  OR (status = 'posted' AND posted_at > ?)"
        ")",
        (guild_id, channel_id, caller_id, now - posted_window_seconds),
    ).fetchall()
    if any(r["prompt_mac"] == mac for r in live):
        return MSG_ALREADY_WORKING
    return MSG_TOO_MANY_HERE if len(live) >= max_asks else None


def queue_refusal(conn: sqlite3.Connection, posts_in_flight: int, max_concurrent: int,
                  max_queued: int) -> str | None:
    """Rows that are posted and waiting for a reply do not count."""
    queued = conn.execute("SELECT count(*) FROM interactions WHERE status = 'queued'").fetchone()[0]
    if queued + posts_in_flight >= max_concurrent + max_queued:
        return MSG_BUSY
    return None
