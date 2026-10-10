"""Reply delivery and the prompt fetch (DESIGN.md, Reply delivery; ADR-0017).

deliver_reply is the single implementation behind the MCP discord_reply tool, and
get_prompt behind the get_prompt tool. Both answer only a connector bound to the
asking member. The reply token is single-use: authorization runs before the claim,
and the claim is one conditional UPDATE. Prompts are held here, in memory only.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Callable

from . import foreign, metrics, registry
from .config import Config
from .db import transaction
from .discord_out import DiscordClient, Outcome
from .sweeper import MSG_NOT_POSTED, finalize_in_tx

log = logging.getLogger(__name__)

MESSAGE_LIMIT = 2000
HARD_SPLIT = 200
MAX_CHUNKS = 5
QUOTE_CHARS = 200
# SpaceXAI's terms require crediting Grok wherever its output is published, in the Brand Guidelines' words
# (DESIGN.md, Rendering and chunking).
ATTRIBUTION = "Written with Grok"

# deliver_reply's results; get_prompt shares the ones it can return and adds OK and RESTARTED.
DELIVERED, BAD_REQUEST, TOO_LONG, UNKNOWN, USED, CLOSED, REVOKED, DISCORD_ERROR = (
    "delivered", "bad_request", "too_long", "unknown", "used", "closed", "revoked", "discord_error")
OK, RESTARTED = "ok", "restarted"
# Internal only: a call through another member's connector that took the foreign ending (T11b). The caller gets
# UNKNOWN, and it counts as UNKNOWN in the tool's result metric (DESIGN.md, Another member's connector).
FOREIGN = "foreign"

MSG_RESTARTED_BEFORE_FETCH = "The bridge restarted before Grok could read your message. Please ask again."
LIVE_STATUSES = ("queued", "posted", "answering")


def mint_reply_token() -> tuple[str, str]:
    """A new `rt_` token (32 random bytes, base64url) and the SHA-256 that is stored."""
    token = "rt_" + secrets.token_urlsafe(32)
    return token, token_hash(token)


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


class HeldPrompts:
    """The prompts of live asks, keyed by interaction id, in memory only: no prompt text is ever stored
    (DESIGN.md, ACK step 13; ADR-0017). A restart loses them, which get_prompt turns into T11a.

    A prompt is held from the spawn, before the worker can mark the row posted (T3), until the interaction
    ends. It's dropped only after the ending commits, so a row is never posted with its prompt gone:
    deliver_reply drops its own after its finish commits, and drop_ended, run at the end of every finalizer
    pass, drops the rest, since every other ending wakes the finalizer after its commit.
    """

    def __init__(self) -> None:
        self._prompts: dict[str, str] = {}

    def __len__(self) -> int:
        return len(self._prompts)

    def hold(self, interaction_id: str, prompt: str) -> None:
        self._prompts[interaction_id] = prompt

    def get(self, interaction_id: str) -> str | None:
        return self._prompts.get(interaction_id)

    def drop(self, interaction_id: str) -> None:
        self._prompts.pop(interaction_id, None)

    def drop_ended(self, conn: sqlite3.Connection) -> None:
        """Drop every prompt whose interaction is no longer live, as committed."""
        if not self._prompts:
            return
        ids = list(self._prompts)
        id_marks = ",".join("?" for _ in ids)
        live_marks = ",".join("?" for _ in LIVE_STATUSES)
        live = {row[0] for row in conn.execute(
            f"SELECT interaction_id FROM interactions WHERE interaction_id IN ({id_marks}) "
            f"AND status IN ({live_marks})", (*ids, *LIVE_STATUSES))}
        for interaction_id in ids:
            if interaction_id not in live:
                del self._prompts[interaction_id]


def _log_foreign(tool: str, interaction_id: str | None, owner: str | None, caller: str) -> None:
    """The one line a foreign ending's call writes, instead of the tool's usual one: membership row ids, never
    Discord ids, the token or the prompt (DESIGN.md, Another member's connector > Observability)."""
    metrics.METRICS.foreign_connector.inc(tool)
    log.warning("%s: foreign (interaction %s, token owner %s, caller %s)", tool, interaction_id or "unknown",
                owner or "unknown", caller)


def quote(prompt: str) -> str:
    """One line, so it can never break out of the answer's quote block."""
    collapsed = " ".join(prompt.split())
    return collapsed[:QUOTE_CHARS] + ("…" if len(collapsed) > QUOTE_CHARS else "")


def render(prompt: str | None, content: str) -> str:
    """Quote line, then the answer; with no prompt held (an answer after a restart), only the answer.
    Mentions never ping."""
    if prompt is None:
        return content
    return "> " + quote(prompt) + "\n" + content


_PIECES = re.compile(rf"\S{{1,{HARD_SPLIT}}}|\s{{1,{HARD_SPLIT}}}")


def chunk(text: str) -> list[str]:
    """Pack greedily on whitespace boundaries into messages of at most 2000 characters.

    Words longer than 200 characters are hard-split every 200, and no piece
    exceeds 200 characters, so every chunk but the last is over 1800.
    """
    chunks: list[str] = []
    current = ""
    for piece in _PIECES.findall(text):
        if len(current) + len(piece) > MESSAGE_LIMIT:
            if current.strip():
                chunks.append(current)
            current = "" if piece.isspace() else piece
        else:
            current += piece
    if current.strip():
        chunks.append(current)
    return chunks


def attribute(chunks: list[str]) -> list[str]:
    """End the answer with the attribution line: on the last message after a blank line, or as a message of its
    own when that doesn't fit. Either way the answer stays within MAX_CHUNKS messages (DESIGN.md, Rendering)."""
    last = chunks[-1].rstrip() + "\n\n" + ATTRIBUTION
    if len(last) <= MESSAGE_LIMIT:
        return chunks[:-1] + [last]
    return chunks + [ATTRIBUTION]


class ReplyService:
    def __init__(self, conn: sqlite3.Connection, discord: DiscordClient, config: Config,
                 wake_finalizer: Callable[[], None], held: HeldPrompts,
                 clock: Callable[[], float] = time.time) -> None:
        self._conn = conn
        self._discord = discord
        self._config = config
        self._wake_finalizer = wake_finalizer
        self._held = held
        self._clock = clock

    @property
    def max_chars(self) -> int:
        return self._config.reply_max_chars

    def get_prompt(self, reply_token: object, mcp_registration_id: str) -> tuple[str, str | None]:
        """DESIGN.md, get_prompt. Returns the result and, for OK, the compact JSON the tool returns.

        Logs exactly one line per call: never the token or the prompt.
        """
        result, interaction_id, text = self._get_prompt(reply_token, mcp_registration_id)
        if result == FOREIGN:
            _log_foreign("get_prompt", interaction_id, text, mcp_registration_id)
            metrics.METRICS.prompt_fetches.inc(UNKNOWN)
            return UNKNOWN, None
        metrics.METRICS.prompt_fetches.inc(result)
        log.log(logging.INFO if result == OK else logging.WARNING, "prompt fetch: %s (interaction %s)",
                result, interaction_id or "unknown")
        return result, text

    def _get_prompt(self, reply_token: object, mcp_registration_id: str) -> tuple[str, str | None, str | None]:
        """For FOREIGN, the third value is the token owner's membership id, for the log line only."""
        if not isinstance(reply_token, str) or not reply_token:
            return BAD_REQUEST, None, None
        now = int(self._clock())
        conn = self._conn
        # The row read, the memory lookup and any ending share one transaction, with no await in between.
        with transaction(conn):
            row = conn.execute("SELECT * FROM interactions WHERE reply_token_hash = ?",
                               (token_hash(reply_token),)).fetchone()
            if row is None:
                return UNKNOWN, None, None
            interaction_id = row["interaction_id"]
            # Membership first, and before recheck: another member's connector learns nothing about this
            # interaction, and a live ask it reaches ends and bans its owner's secret (T11b).
            if mcp_registration_id != row["registration_id"]:
                if not foreign.foreign_ending_in_tx(conn, row, mcp_registration_id, self._config.membership, now):
                    return UNKNOWN, None, None
                result, owner, ended = FOREIGN, row["registration_id"], True
            elif row["status"] != "posted" or row["reply_expires_at"] <= now:
                return CLOSED, interaction_id, None
            else:
                owner = None
                if registry.recheck(conn, row, self._config.guild_allowlist):
                    result, message = REVOKED, registry.MSG_WITHDRAWN
                else:
                    prompt = self._held.get(interaction_id)
                    if prompt is not None:
                        return OK, interaction_id, json.dumps(
                            {"prompt": prompt, "discord_user_id": row["caller_id"], "channel_id": row["channel_id"]},
                            separators=(",", ":"), ensure_ascii=False)
                    # Lost to a restart (T11a).
                    result, message = RESTARTED, MSG_RESTARTED_BEFORE_FETCH
                # A guard: inside this transaction the row can't leave posted, but finalize moves only a row still
                # posted, so an answered ask could never be reported as withdrawn or restarted.
                ended = finalize_in_tx(conn, interaction_id, ("posted",), "failed", message, now)
        if not ended:
            return CLOSED, interaction_id, None
        self._wake_finalizer()
        return result, interaction_id, owner

    async def deliver_reply(self, reply_token: object, content: object, mcp_registration_id: str) -> str:
        """Deliver, then log exactly one line per attempt: never the token or the content."""
        result = await self._deliver(reply_token, content, mcp_registration_id)
        interaction_id = None
        if isinstance(reply_token, str) and reply_token:
            found = self._conn.execute("SELECT interaction_id, registration_id, posted_at FROM interactions "
                                       "WHERE reply_token_hash = ?", (token_hash(reply_token),)).fetchone()
            interaction_id = found["interaction_id"] if found else None
            if result == FOREIGN:
                _log_foreign("discord_reply", interaction_id, found["registration_id"], mcp_registration_id)
                metrics.METRICS.replies.inc(UNKNOWN)
                return UNKNOWN
            if result == DELIVERED and found and found["posted_at"]:
                metrics.METRICS.reply_wait.observe(max(0.0, self._clock() - found["posted_at"]))
        metrics.METRICS.replies.inc(result)
        chars = len(content) if isinstance(content, str) else None
        log.log(logging.INFO if result == DELIVERED else logging.WARNING,
                "reply: %s (interaction %s, %s chars)", result, interaction_id or "unknown", chars)
        return result

    async def _deliver(self, reply_token: object, content: object, mcp_registration_id: str) -> str:
        # 1. Validate. Nothing is written and the token is not consumed.
        if not isinstance(reply_token, str) or not reply_token or not isinstance(content, str) \
                or not content.strip():
            return BAD_REQUEST
        if len(content) > self._config.reply_max_chars:
            return TOO_LONG

        # 2. Authorize, then claim, in one transaction.
        now = int(self._clock())
        conn = self._conn
        with transaction(conn):
            row = conn.execute("SELECT * FROM interactions WHERE reply_token_hash = ?",
                               (token_hash(reply_token),)).fetchone()
            if row is None:
                return UNKNOWN
            if mcp_registration_id != row["registration_id"]:
                # The same evidence as in get_prompt, for a run that skips the fetch (T11b).
                if not foreign.foreign_ending_in_tx(conn, row, mcp_registration_id, self._config.membership, now):
                    return UNKNOWN
                ended_foreign, withdrawn = True, False
            elif row["status"] in ("answering", "answered"):
                return USED
            elif row["status"] != "posted" or row["reply_expires_at"] <= now:
                return CLOSED
            elif registry.recheck(conn, row, self._config.guild_allowlist):
                if finalize_in_tx(conn, row["interaction_id"], ("posted",), "failed",
                                  registry.MSG_WITHDRAWN, now):
                    ended_foreign, withdrawn = False, True
                else:
                    return CLOSED
            else:
                ended_foreign, withdrawn = False, False
                # The quote comes from the held prompt; after a restart there is none (DESIGN.md, Rendering).
                prompt = self._held.get(row["interaction_id"])
                conn.execute(
                    "UPDATE interactions SET status = 'answering', chunks_sent = 0, updated_at = ? "
                    "WHERE interaction_id = ? AND status = 'posted'", (now, row["interaction_id"]))
        if ended_foreign:
            self._wake_finalizer()
            return FOREIGN
        if withdrawn:
            self._wake_finalizer()
            return REVOKED

        # 3. Deliver: chunk 1 edits the deferred message, the rest are followups.
        interaction_id = row["interaction_id"]
        chunks = attribute(chunk(render(prompt, content)))
        for index, text in enumerate(chunks[:MAX_CHUNKS]):
            if index == 0:
                result = await self._discord.edit_original(row["interaction_token"], text)
            else:
                result = await self._discord.create_followup(row["interaction_token"], text,
                                                              ephemeral=bool(row["private"]))
            if result.outcome is not Outcome.OK:
                return self._discord_failed(interaction_id, sent=index)
            with transaction(conn):
                conn.execute("UPDATE interactions SET chunks_sent = chunks_sent + 1 WHERE interaction_id = ?",
                             (interaction_id,))

        # 4. Finish.
        with transaction(conn):
            conn.execute(
                "UPDATE interactions SET status = 'answered', interaction_token = NULL, updated_at = ? "
                "WHERE interaction_id = ? AND status = 'answering'", (int(self._clock()), interaction_id))
            registry.touch_in_tx(conn, row["registration_id"], int(self._clock()))  # a delivered answer is use
        self._held.drop(interaction_id)  # only after the commit
        return DELIVERED

    def _discord_failed(self, interaction_id: str, sent: int) -> str:
        now = int(self._clock())
        with transaction(self._conn):
            if sent > 0:
                # Partial answer: leave it up; the token cannot finish it (T15).
                self._conn.execute(
                    "UPDATE interactions SET status = 'failed', interaction_token = NULL, updated_at = ? "
                    "WHERE interaction_id = ? AND status = 'answering'", (now, interaction_id))
                wake = False
            else:
                # Nothing landed: replace "thinking..." with an error (T16).
                wake = finalize_in_tx(self._conn, interaction_id, ("answering",), "failed", MSG_NOT_POSTED, now)
        self._held.drop(interaction_id)  # only after the commit
        if wake:
            self._wake_finalizer()
        log.warning("reply for interaction %s could not be posted to Discord after %d chunk(s)",
                    interaction_id, sent)
        return DISCORD_ERROR
