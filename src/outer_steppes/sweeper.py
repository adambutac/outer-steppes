"""finalize(), the finalizer, startup reconciliation, and the sweeper.

DESIGN.md, "Final messages", "Idempotency", and "Interaction state machine".
Every path that ends an /ask without an answer goes through finalize(): one
conditional UPDATE that sets the status and the pending final message. Only
the finalizer clears the interaction token, after the PATCH has been sent or
can no longer be sent.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Collection

from . import metrics
from .db import transaction
from .discord_out import DiscordClient, Outcome
# Module imports, not names: oauth imports registry, which imports this module.
from . import oauth, registry, sessions

log = logging.getLogger(__name__)

DISCORD_EPOCH_MS = 1_420_070_400_000
TOKEN_USABLE_SECONDS = 890        # Discord tokens last 900 s; keep a margin
# A 404 this soon after our ACK may only mean Discord has not processed the
# deferral yet (a tunnel adds latency). Measured from the row's created_at,
# which is our own clock at ACK time: comparing our clock with the Discord
# timestamp in the interaction id would let clock skew eat the window.
DEFERRAL_RACE_SECONDS = 5
DEFERRAL_RACE_RETRY_DELAYS = (1.0, 2.0)
SWEEP_INTERVAL_SECONDS = 30

MSG_RESTARTED = "The bridge restarted before sending this. Please ask again."
MSG_NOT_POSTED = "The answer couldn't be posted to Discord."
MSG_BUSY = "Busy, try again."


def snowflake_time(snowflake: str) -> float:
    """Creation time, in Unix seconds, encoded in a Discord id."""
    return ((int(snowflake) >> 22) + DISCORD_EPOCH_MS) / 1000


def finalize_in_tx(conn: sqlite3.Connection, interaction_id: str, from_statuses: Collection[str],
                   to_status: str, message: str, now: float) -> bool:
    """The conditional UPDATE, for callers already inside a transaction.

    Returns False if the row was not in one of `from_statuses`: another
    writer won, and the caller must make no further writes or Discord calls.
    """
    marks = ",".join("?" for _ in from_statuses)
    cur = conn.execute(
        f"UPDATE interactions SET status = ?, final_message = ?, updated_at = ? "
        f"WHERE interaction_id = ? AND status IN ({marks})",
        (to_status, message, int(now), interaction_id, *from_statuses),
    )
    return cur.rowcount == 1


def finalize(conn: sqlite3.Connection, interaction_id: str, from_statuses: Collection[str],
             to_status: str, message: str, now: float) -> bool:
    with transaction(conn):
        return finalize_in_tx(conn, interaction_id, from_statuses, to_status, message, now)


class Finalizer:
    """Sends pending final messages. Woken after commits, on sweeps, at startup."""

    def __init__(self, conn: sqlite3.Connection, discord: DiscordClient,
                 clock: Callable[[], float] = time.time,
                 sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
                 on_pass: Callable[[], None] = lambda: None) -> None:
        self._conn = conn
        # Run at the end of every pass, on its own. Every ending but an answer wakes the finalizer after its commit,
        # and the sweeper runs a pass after its own, so main.py drops the held prompts of ended
        # interactions here (DESIGN.md, ACK step 13).
        self._on_pass = on_pass
        self._discord = discord
        self._clock = clock
        self._sleep = sleep
        self._wake = asyncio.Event()
        self._lock = asyncio.Lock()  # sweeper and wake-ups must not double-PATCH

    def wake(self) -> None:
        self._wake.set()

    async def run_forever(self) -> None:
        while True:
            await self._wake.wait()
            self._wake.clear()
            try:
                await self.run_once()
            except Exception:
                log.exception("finalizer pass failed")

    async def run_once(self) -> int:
        """Attempt every pending final message once. Returns how many were cleared."""
        async with self._lock:
            rows = self._conn.execute(
                "SELECT interaction_id, interaction_token, final_message, created_at FROM interactions "
                "WHERE final_message IS NOT NULL AND interaction_token IS NOT NULL "
                "AND status <> 'answered'"
            ).fetchall()
            cleared = 0
            for row in rows:
                if await self._deliver(row["interaction_id"], row["interaction_token"],
                                       row["final_message"], row["created_at"]):
                    cleared += 1
            try:
                self._on_pass()  # after the sends, so a failure here never holds back a final message
            except Exception:
                log.exception("dropping ended prompts failed")
            return cleared

    async def _deliver(self, interaction_id: str, token: str, message: str, acked_at: int) -> bool:
        if snowflake_time(interaction_id) + TOKEN_USABLE_SECONDS <= self._clock():
            self._clear(interaction_id)
            return True

        result = await self._discord.edit_original(token, message)
        for delay in DEFERRAL_RACE_RETRY_DELAYS:
            if (result.outcome is not Outcome.NOT_FOUND
                    or self._clock() - acked_at >= DEFERRAL_RACE_SECONDS):
                break
            # Discord may not have processed the deferral yet: retry.
            await self._sleep(delay)
            result = await self._discord.edit_original(token, message)

        if result.outcome is Outcome.OK:
            self._clear(interaction_id)
            return True
        if result.outcome in (Outcome.NOT_FOUND, Outcome.UNAUTHORIZED):
            # The message can no longer be edited. Say so: the user is left on
            # "thinking..." until Discord expires the interaction.
            log.warning(
                "final message not delivered for interaction %s: Discord returned %s; giving up. "
                "A 401 on a fresh interaction usually means DISCORD_APPLICATION_ID is wrong.",
                interaction_id, result.status,
            )
            self._clear(interaction_id)
            return True
        log.warning("final message for interaction %s failed (status %s); will retry on the next sweep",
                    interaction_id, result.status)
        return False  # leave it for the next pass

    def _clear(self, interaction_id: str) -> None:
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE interactions SET final_message = NULL, interaction_token = NULL, updated_at = ? "
                "WHERE interaction_id = ? AND final_message IS NOT NULL",
                (int(self._clock()), interaction_id),
            )


def reconcile_startup(conn: sqlite3.Connection, now: float) -> dict[str, int]:
    """Run before either listener opens. Tasks and prompts died with the process."""
    counts = {"queued": 0, "answering_unsent": 0, "answering_partial": 0, "claimed": 0}
    with transaction(conn):
        for row in conn.execute("SELECT interaction_id FROM interactions WHERE status = 'queued'").fetchall():
            if finalize_in_tx(conn, row["interaction_id"], ("queued",), "failed", MSG_RESTARTED, now):
                counts["queued"] += 1

        for row in conn.execute(
            "SELECT interaction_id, chunks_sent FROM interactions WHERE status = 'answering'"
        ).fetchall():
            if row["chunks_sent"] == 0:
                finalize_in_tx(conn, row["interaction_id"], ("answering",), "failed", MSG_NOT_POSTED, now)
                counts["answering_unsent"] += 1
            else:
                conn.execute(
                    "UPDATE interactions SET status = 'failed', interaction_token = NULL, updated_at = ? "
                    "WHERE interaction_id = ? AND status = 'answering'",
                    (int(now), row["interaction_id"]),
                )
                counts["answering_partial"] += 1

        claimed = conn.execute(
            "UPDATE interactions SET status = 'done', updated_at = ? WHERE status = 'claimed'", (int(now),)
        ).rowcount
        counts["claimed"] = claimed
    if counts["claimed"]:
        log.error("found %d committed 'claimed' interaction rows; marked done", counts["claimed"])
    return counts


# Every answer arrives through the connector (ADR-0017), and the bridge can't see it removed inside grok.com, or
# missing from the account that owns the trigger (DESIGN.md, Connector status), so the timeout names what to check.
MSG_NO_ANSWER = ("No answer arrived from Grok in time. Check that **Outer Steppes** is connected in the grok.com "
                 "account that owns your automation, and add or reconnect it there.")
MSG_UNCONFIRMED_NO_ANSWER = "Grok didn't confirm the request, and no answer arrived."


def expire_replies(conn: sqlite3.Connection, now: float) -> int:
    """T12: posted rows whose reply window has closed. The message depends on post_outcome."""
    expired = 0
    rows = conn.execute(
        "SELECT interaction_id, post_outcome FROM interactions WHERE status = 'posted' AND reply_expires_at <= ?",
        (int(now),)
    ).fetchall()
    for row in rows:
        # 'pending' means the worker never classified a response (crash, or the POST never left).
        message = MSG_NO_ANSWER if row["post_outcome"] == "accepted" else MSG_UNCONFIRMED_NO_ANSWER
        if finalize(conn, row["interaction_id"], ("posted",), "expired", message, now):
            expired += 1
    return expired


def sweep_orphans(conn: sqlite3.Connection, live_task_ids: Collection[str], queue_wait_seconds: int,
                  now: float) -> int:
    """Fail `queued` rows whose task died between commit and spawn (T7)."""
    cutoff = int(now) - (queue_wait_seconds + 10)
    failed = 0
    rows = conn.execute(
        "SELECT interaction_id FROM interactions WHERE status = 'queued' AND created_at < ?", (cutoff,)
    ).fetchall()
    for row in rows:
        if row["interaction_id"] in live_task_ids:
            continue  # live tasks enforce their own deadline; never cancel them
        if finalize(conn, row["interaction_id"], ("queued",), "failed", MSG_BUSY, now):
            failed += 1
    return failed


RETENTION_INTERVAL_SECONDS = 3600
INTERACTION_RETENTION_SECONDS = 86_400
RATE_EVENT_RETENTION_SECONDS = 2 * 86_400
REVOKED_REGISTRATION_RETENTION_SECONDS = 30 * 86_400
REMOVED_AUTOMATION_RETENTION_SECONDS = 30 * 86_400
TERMINAL_STATUSES = ("done", "answered", "failed", "expired")


def expire_idle_members_in_tx(conn: sqlite3.Connection, now: int, idle_days: int) -> int:
    """End the membership of every member idle for `idle_days`, exactly as /leave does.

    0 disables it. A member whose last_used_at is NULL never expires: creation sets it,
    so that can't happen, and it keeps a bug from expiring everyone. Returns how many left.
    """
    if idle_days <= 0:
        return 0
    cutoff = now - idle_days * 86_400
    idle = conn.execute("SELECT id FROM registrations WHERE revoked_at IS NULL AND last_used_at IS NOT NULL "
                        "AND last_used_at < ?", (cutoff,)).fetchall()
    for member in idle:
        registry.revoke_in_tx(conn, member["id"], now)
    return len(idle)


# member_idle_days defaults to 0 (off) here and in run_sweeper on purpose: expiry ends memberships, so a caller
# that doesn't pass it must not expire anyone. The real value comes from config (main.py passes it, and a test
# checks that); the config default is 180.
def apply_retention(conn: sqlite3.Connection, now: float, member_idle_days: int = 0) -> dict[str, int]:
    """DESIGN.md, Data model > Retention. Live rows and rows with a pending final message are kept.

    Idle members are ended first (PR 8d-1), so their rows enter the 30-day revoked retention.
    The finalizer sends their withdrawn final messages in the same sweeper pass.
    """
    now = int(now)
    marks = ",".join("?" for _ in TERMINAL_STATUSES)
    with transaction(conn):
        idle_members = expire_idle_members_in_tx(conn, now, member_idle_days)
        interactions = conn.execute(
            f"DELETE FROM interactions WHERE created_at < ? AND status IN ({marks}) AND final_message IS NULL",
            (now - INTERACTION_RETENTION_SECONDS, *TERMINAL_STATUSES)).rowcount
        rate_events = conn.execute("DELETE FROM rate_events WHERE at < ?",
                                   (now - RATE_EVENT_RETENTION_SECONDS,)).rowcount
        registrations = conn.execute(
            "DELETE FROM registrations WHERE revoked_at IS NOT NULL AND revoked_at < ?",
            (now - REVOKED_REGISTRATION_RETENTION_SECONDS,)).rowcount
        # Removed automations of members who stay (a revoked member's go with it, by cascade).
        # Their URL and secret were wiped on removal; this only stops the table growing.
        automations = conn.execute(
            "DELETE FROM automations WHERE removed_at IS NOT NULL AND removed_at < ?",
            (now - REMOVED_AUTOMATION_RETENTION_SECONDS,)).rowcount
        # Expired account-page sessions, and save counts older than the rate-limit window.
        account = sessions.delete_expired_in_tx(conn, now)
    if idle_members:  # counted only after the commit
        metrics.METRICS.membership_changes.inc("self", "idle_left", amount=idle_members)
    return {"interactions": interactions, "rate_events": rate_events, "registrations": registrations,
            "automations": automations, "idle_members": idle_members, **account}


async def run_sweeper(conn: sqlite3.Connection, finalizer: Finalizer,
                      live_task_ids: Callable[[], Collection[str]], queue_wait_seconds: int,
                      clock: Callable[[], float] = time.time, member_idle_days: int = 0,
                      interval: float = SWEEP_INTERVAL_SECONDS) -> None:
    last_retention = float("-inf")
    while True:
        await asyncio.sleep(interval)
        try:
            if clock() - last_retention >= RETENTION_INTERVAL_SECONDS:
                removed = apply_retention(conn, clock(), member_idle_days)
                last_retention = clock()
                if any(removed.values()):
                    log.info("retention removed %s", removed)
            expire_replies(conn, clock())
            sweep_orphans(conn, live_task_ids(), queue_wait_seconds, clock())
            with transaction(conn):
                oauth.delete_expired_in_tx(conn, int(clock()))
            await finalizer.run_once()
        except Exception:
            log.exception("sweeper pass failed")
