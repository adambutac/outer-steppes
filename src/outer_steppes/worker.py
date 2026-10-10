"""Worker tasks (DESIGN.md, Worker and Interaction state machine T3-T9).

A worker never PATCHes Discord directly: every ending goes through
finalize(), and only when its own conditional update changed the row.
Whichever code path made the row terminal owns its final message.
"""

from __future__ import annotations

import asyncio
import logging
import sqlite3
import time
from collections.abc import Callable, Collection
from typing import Protocol

from . import foreign, metrics, registry
from .automation_client import PostClass, PostOutcome, build_payload
from .config import Config
from .db import transaction
from .reply import mint_reply_token
from .sweeper import MSG_BUSY, Finalizer, finalize, finalize_in_tx, snowflake_time
from .url_policy import AcceptedUrl, Resolver, UrlRejected, system_resolver, validate_automation_url

log = logging.getLogger(__name__)

MSG_URL_REJECTED = "That automation URL was rejected."


class Poster(Protocol):
    async def post(self, accepted: AcceptedUrl, body: bytes, secret: str) -> PostOutcome: ...


def _post_result(outcome: PostOutcome) -> str:
    """The fixed metric label for an automation POST outcome (DESIGN.md, Observability)."""
    if outcome.kind is PostClass.ACCEPTED:
        return "accepted"
    if outcome.kind is PostClass.AMBIGUOUS:
        return "ambiguous"
    if outcome.status in (429, 503):
        return "busy"
    if outcome.status == 401:
        return "rejected_auth"
    return "not_sent" if outcome.status is None else "rejected_other"


class WorkerPool:
    def __init__(self, conn: sqlite3.Connection, finalizer: Finalizer, config: Config, poster: Poster,
                 resolver: Resolver = system_resolver, bind_hosts: Collection[str] = (),
                 clock: Callable[[], float] = time.time) -> None:
        self._conn = conn
        self._finalizer = finalizer
        self._config = config
        self._poster = poster
        self._resolver = resolver
        self._bind_hosts = frozenset(bind_hosts)
        self._clock = clock
        self._semaphore = asyncio.Semaphore(config.max_concurrent_automation_posts)
        self._tasks: dict[str, asyncio.Task] = {}
        # Workers between the T3 commit and the end of step 5 (the queue cap counts them).
        self.posts_in_flight = 0

    def live_task_ids(self) -> set[str]:
        return set(self._tasks)

    def spawn(self, interaction_id: str) -> None:
        """Start the worker. Called only after the ACK response has been written and the prompt is held."""
        task = asyncio.create_task(self._run(interaction_id), name=f"worker-{interaction_id}")
        self._tasks[interaction_id] = task

    async def close(self) -> None:
        tasks = list(self._tasks.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ steps
    async def _run(self, interaction_id: str) -> None:
        try:
            # Step 1: wait for a slot.
            waited_from = time.monotonic()
            try:
                await asyncio.wait_for(self._semaphore.acquire(), self._config.queue_wait_seconds)
            except TimeoutError:
                metrics.METRICS.queue_wait.observe(time.monotonic() - waited_from)
                self._end(interaction_id, ("queued",), MSG_BUSY)
                return
            metrics.METRICS.queue_wait.observe(time.monotonic() - waited_from)
            try:
                await self._work(interaction_id)
            finally:
                self._semaphore.release()
        except Exception:
            log.exception("worker failed", extra={"interaction_id": interaction_id})
        finally:
            self._tasks.pop(interaction_id, None)

    async def _work(self, interaction_id: str) -> None:
        row = self._row(interaction_id)
        if row is None or row["status"] != "queued":
            return  # checkpoint: someone else finalized it

        # Step 2: DNS and URL policy, outside any transaction, without the secret.
        # The URL comes from the automation this /ask was accepted for.
        url = self._conn.execute("SELECT automation_url FROM automations WHERE id = ? AND removed_at IS NULL",
                                 (row["automation_id"],)).fetchone()
        try:
            accepted = await validate_automation_url(url["automation_url"] if url else None,
                                                     self._bind_hosts, self._resolver)
        except UrlRejected as exc:
            log.warning("automation URL rejected before POST: %s", exc, extra={"interaction_id": interaction_id})
            metrics.METRICS.automation_posts.inc("url_rejected")
            self._end(interaction_id, ("queued",), MSG_URL_REJECTED)
            return
        if self._status(interaction_id) != "queued":
            return  # checkpoint

        # Step 3: re-check and copy, in one transaction.
        now = int(self._clock())
        reply_token, reply_hash = mint_reply_token()
        wake = False
        with transaction(self._conn):
            row = self._row(interaction_id)
            if row is None or row["status"] != "queued":
                return
            refusal = registry.recheck(self._conn, row, self._config.guild_allowlist)
            # recheck already requires this automation to be the member's active one.
            # Automations are never updated in place, so the URL can't have changed
            # after DNS; comparing it anyway costs nothing.
            automation = self._conn.execute(
                "SELECT automation_url, signing_secret FROM automations WHERE id = ? AND removed_at IS NULL",
                (row["automation_id"],)).fetchone()
            if refusal is None and (automation is None or automation["automation_url"] != accepted.url):
                refusal = registry.MSG_NOT_AUTHORIZED
            # Only here, not in recheck: asks already queued aren't sent to the account whose run caused the ban,
            # but a run already started may still reach the member's own connector (DESIGN.md, Re-checks).
            if refusal is None and foreign.automation_banned(self._conn, row["automation_id"]):
                refusal = foreign.text(foreign.MSG_BLOCKED, self._config.membership)
            if refusal is not None:
                wake = finalize_in_tx(self._conn, interaction_id, ("queued",), "failed", refusal, now)
            else:
                secret = automation["signing_secret"]
                expires_at = int(snowflake_time(interaction_id)) + self._config.reply_ttl_seconds
                self._conn.execute(
                    "UPDATE interactions SET status = 'posted', reply_token_hash = ?, reply_expires_at = ?, "
                    "post_outcome = 'pending', posted_at = ?, updated_at = ? "
                    "WHERE interaction_id = ? AND status = 'queued'",
                    (reply_hash, expires_at, now, now, interaction_id))
        if refusal is not None:
            if wake:
                self._finalizer.wake()
            return

        # Step 4: sign and POST once. The secret lives only in this frame.
        self.posts_in_flight += 1
        try:
            body = build_payload(correlation_id=row["correlation_id"], reply_token=reply_token,
                                 reply_expires_at=expires_at)
            posted_from = time.monotonic()
            outcome = await self._poster.post(accepted, body, secret)
            metrics.METRICS.automation_latency.observe(time.monotonic() - posted_from)
            metrics.METRICS.automation_posts.inc(_post_result(outcome))
        finally:
            self.posts_in_flight -= 1
            del secret, reply_token

        # Step 5: classify. Every update is conditional on status='posted'.
        log.info("automation POST: %s (HTTP %s)", outcome.kind.value, outcome.status,
                 extra={"interaction_id": interaction_id, "correlation_id": row["correlation_id"]})
        if outcome.kind is PostClass.NOT_SENT:
            self._end(interaction_id, ("posted",), outcome.message)
            return
        with transaction(self._conn):
            self._conn.execute(
                "UPDATE interactions SET post_outcome = ?, updated_at = ? "
                "WHERE interaction_id = ? AND status = 'posted'",
                (outcome.kind.value, int(self._clock()), interaction_id))

    # ------------------------------------------------------------------ helpers
    def _row(self, interaction_id: str) -> sqlite3.Row | None:
        return self._conn.execute("SELECT * FROM interactions WHERE interaction_id = ?",
                                  (interaction_id,)).fetchone()

    def _status(self, interaction_id: str) -> str | None:
        row = self._row(interaction_id)
        return row["status"] if row else None

    def _end(self, interaction_id: str, from_statuses: tuple[str, ...], message: str) -> None:
        if finalize(self._conn, interaction_id, from_statuses, "failed", message, self._clock()):
            self._finalizer.wake()
