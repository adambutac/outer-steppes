"""The ACK path for signed interactions (DESIGN.md, ACK steps 4-12).

Everything from the dedup claim to acceptance runs in one BEGIN IMMEDIATE
transaction under one process lock, with no network I/O. The HTTP layer
writes the response and only then spawns the worker (step 13).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import secrets
import sqlite3
import time
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from . import commands, foreign, metrics, policy, registry
from .config import Config

log = logging.getLogger(__name__)

INTERACTION_APPLICATION_COMMAND = 2
SUBCOMMAND = 1
RESPONSE_CHANNEL_MESSAGE = 4
RESPONSE_DEFERRED_CHANNEL_MESSAGE = 5
FLAG_EPHEMERAL = 64
FLAG_SUPPRESS_EMBEDS = 4

MSG_UNSUPPORTED_INTERACTION = "Unsupported interaction."
MSG_UNSUPPORTED_INVOCATION = "Unsupported invocation."
MSG_UNSUPPORTED_COMMAND = "Unsupported command."
MSG_ALREADY_HANDLED = "Already handled."
MSG_MISCONFIGURED = "The bridge is misconfigured. Tell the operator to check the server log."



def ephemeral_message(content: str) -> dict:
    """The single builder for type 4 responses, original or replayed.

    A reply with a link (today only /webhook set's) suppresses embeds, so Discord
    doesn't fetch a preview of the account page. Deriving it from the content keeps
    a replayed answer identical, since only ack_content is stored.
    """
    flags = FLAG_EPHEMERAL | FLAG_SUPPRESS_EMBEDS if "https://" in content else FLAG_EPHEMERAL
    return {
        "type": RESPONSE_CHANNEL_MESSAGE,
        "data": {
            "content": content,
            "flags": flags,
            "allowed_mentions": {"parse": []},
        },
    }


def deferred_response(private: bool) -> dict:
    """The single builder for type 5 responses, original or replayed."""
    body: dict = {"type": RESPONSE_DEFERRED_CHANNEL_MESSAGE}
    if private:
        body["data"] = {"flags": FLAG_EPHEMERAL}
    return body


def prompt_mac(key: bytes, prompt: str) -> str:
    """The repeat check's value: HMAC-SHA256 of the prompt with whitespace runs collapsed (DESIGN.md, ACK step 12).
    The key is random per process and never stored, so a short prompt can't be recovered from the database by
    guessing; no prompt text is stored (ADR-0017)."""
    return hmac.new(key, " ".join(prompt.split()).encode("utf-8"), hashlib.sha256).hexdigest()


def _obj(value: object) -> dict:
    """Treat any non-object field as empty rather than crashing the handler."""
    return value if isinstance(value, dict) else {}


CONTEXT_GUILD, CONTEXT_BOT_DM, CONTEXT_PRIVATE_CHANNEL = 0, 1, 2
GUILD_INSTALL, USER_INSTALL = "0", "1"


@dataclass(frozen=True)
class Invocation:
    caller_id: str
    guild_id: str | None      # None outside servers
    context: int              # 0 server, 1 DM with the bot, 2 group DM / other DM
    guild_installed: bool     # the app is installed in this server


def resolve_invocation(interaction: dict) -> Invocation | None:
    """Who is calling, and from where. Only signed fields are used.

    In a server the caller is member.user.id; in DMs Discord sends a top-level
    user instead. A payload with both must agree; one with neither, or with a
    server but no member, is refused.
    """
    member_user = _obj(_obj(interaction.get("member")).get("user")).get("id") if "member" in interaction else None
    top_user = _obj(interaction.get("user")).get("id") if "user" in interaction else None
    guild_id = interaction.get("guild_id")

    if guild_id is not None:
        if not isinstance(guild_id, str) or not guild_id.isdigit() or member_user is None:
            return None
        if top_user is not None and top_user != member_user:
            return None
        caller_id = member_user
    else:
        if member_user is not None:
            return None
        caller_id = top_user
    if not isinstance(caller_id, str) or not caller_id.isdigit():
        return None

    context = interaction.get("context")
    if context not in (CONTEXT_GUILD, CONTEXT_BOT_DM, CONTEXT_PRIVATE_CHANNEL):
        context = CONTEXT_GUILD if guild_id is not None else CONTEXT_BOT_DM
    if (context == CONTEXT_GUILD) != (guild_id is not None):
        return None

    owners = interaction.get("authorizing_integration_owners")
    if isinstance(owners, dict):
        guild_installed = guild_id is not None and owners.get(GUILD_INSTALL) == guild_id
    else:
        guild_installed = guild_id is not None  # older payloads: guild-only installs
    return Invocation(caller_id, guild_id, context, guild_installed)


@dataclass(frozen=True)
class Spawn:
    interaction_id: str
    prompt: str


@dataclass(frozen=True)
class AckResult:
    body: dict
    spawn: Spawn | None = None
    # Wake the finalizer after the response is written: /leave or /webhook remove
    # withdrew in-flight interactions whose final messages are now pending.
    wake_finalizer: bool = False
    membership_change: str | None = None


class InteractionHandler:
    def __init__(self, config: Config, conn: sqlite3.Connection,
                 posts_in_flight: Callable[[], int],
                 clock: Callable[[], float] = time.time, prompt_key: bytes | None = None) -> None:
        self._config = config
        self._conn = conn
        self._posts_in_flight = posts_in_flight
        self._clock = clock
        self._lock = asyncio.Lock()
        # prompt_mac's key: made at startup, never stored, so after a restart the repeat check doesn't match
        # asks from before it (an accepted gap, DESIGN.md, ACK step 11).
        self._prompt_key = prompt_key or secrets.token_bytes(32)  # tests pass one in

    async def handle(self, interaction: dict) -> AckResult:
        interaction_id = interaction.get("id")
        if (interaction["type"] != INTERACTION_APPLICATION_COMMAND
                or not isinstance(interaction_id, str) or not interaction_id.isdigit()):
            return AckResult(ephemeral_message(MSG_UNSUPPORTED_INTERACTION))

        # The signed body names the application it belongs to. If config names a
        # different one, every later edit of this interaction would fail (Discord
        # answers 401), leaving the user on "thinking..." until the token expires.
        # Refuse up front instead, before any database write.
        if interaction.get("application_id") != self._config.discord_application_id:
            log.error(
                "DISCORD_APPLICATION_ID does not match the application that sent this "
                "interaction; fix the server's DISCORD_APPLICATION_ID (General Information > "
                "Application ID in the Developer Portal)"
            )
            return AckResult(ephemeral_message(MSG_MISCONFIGURED))

        async with self._lock:
            try:
                self._conn.execute("BEGIN IMMEDIATE")
            except sqlite3.OperationalError:
                # Busy beyond the 200 ms timeout. Nothing was written.
                return AckResult(ephemeral_message(policy.MSG_BUSY))
            try:
                result = self._decide(interaction_id, interaction, int(self._clock()))
            except BaseException:
                self._conn.execute("ROLLBACK")
                raise
            self._conn.execute("COMMIT")
        if result.membership_change:
            metrics.METRICS.membership_changes.inc("self", result.membership_change)
        return result

    # ------------------------------------------------------------------ steps 6-12
    def _decide(self, interaction_id: str, interaction: dict, now: int) -> AckResult:
        conn = self._conn

        # Step 6: dedup claim, before any policy check.
        claimed = conn.execute(
            "INSERT INTO interactions (interaction_id, status, ack_kind, created_at, updated_at) "
            "VALUES (?, 'claimed', 'ephemeral', ?, ?) ON CONFLICT (interaction_id) DO NOTHING",
            (interaction_id, now, now),
        ).rowcount == 1
        if not claimed:
            return self._replay(interaction_id)

        def done(message: str, wake_finalizer: bool = False, membership_change: str | None = None) -> AckResult:
            conn.execute(
                "UPDATE interactions SET status = 'done', ack_kind = 'ephemeral', ack_content = ?, "
                "updated_at = ? WHERE interaction_id = ?",
                (message, now, interaction_id),
            )
            return AckResult(ephemeral_message(message), wake_finalizer=wake_finalizer,
                             membership_change=membership_change)

        # Step 7: identity and install context (DESIGN.md, Install contexts).
        invocation = resolve_invocation(interaction)
        if invocation is None:
            return done(MSG_UNSUPPORTED_INVOCATION)
        caller_id, guild_id = invocation.caller_id, invocation.guild_id

        data = _obj(interaction.get("data"))
        name = data.get("name")
        raw_options = data.get("options")
        options = {
            _obj(opt).get("name"): _obj(opt).get("value")
            for opt in (raw_options if isinstance(raw_options, list) else [])
        }
        subcommand = next((_obj(opt).get("name") for opt in (raw_options if isinstance(raw_options, list) else [])
                           if _obj(opt).get("type") == SUBCOMMAND), None)

        # Step 8: leaving and removing run before allowlists and pause, so the
        # kill switch never blocks them.
        config = self._config
        if name == "leave" or (name == "webhook" and subcommand == "remove"):
            if name == "leave":
                result = commands.leave(conn, caller_id, options.get("confirm"), now, config.membership)
            else:
                result = commands.webhook_remove(conn, caller_id, now, config.membership)
            return done(result.message, result.wake_finalizer, result.membership_change)

        # Step 9: allowlists, then pause.
        channel = _obj(interaction.get("channel"))
        channel_id = interaction.get("channel_id") or channel.get("id")
        refusal = policy.allowlist_refusal(
            self._config, guild_id, invocation.guild_installed,
            policy.parent_channel_id(channel, channel_id), caller_id
        )
        if refusal:
            return done(refusal)
        answers_while_paused = name in ("status", "settings") or (name == "webhook" and subcommand == "status")
        if not answers_while_paused and policy.is_paused(conn):
            return done(policy.MSG_PAUSED)

        # Step 10: branch.
        if name == "status":
            return done(commands.status(conn, caller_id, config.membership, now=now).message)
        if name == "settings":
            return done(commands.settings(conn, caller_id, options.get("private"), now).message)
        if name == "webhook" and subcommand == "status":
            return done(commands.webhook_status(conn, caller_id, config.membership, now).message)
        if name == "webhook" and subcommand == "set":
            return done(commands.webhook_set(config.public_base_url, config.membership).message)
        if name != "ask":
            return done(MSG_UNSUPPORTED_COMMAND)

        prompt = options.get("prompt")
        token = interaction.get("token")
        if not isinstance(prompt, str) or not prompt.strip() or not isinstance(token, str) or not token:
            return done(MSG_UNSUPPORTED_INVOCATION)
        # Explicit private wins; otherwise the member's /settings default, which is
        # private until they change it. The same everywhere, whatever the install.
        explicit = options.get("private")
        private = explicit if isinstance(explicit, bool) else commands.answers_private(conn, caller_id)

        # Step 11: admission. Only the caller's own automation routes (ADR-0009).
        route = registry.resolve_route(conn, caller_id, config.membership)
        if isinstance(route, registry.Refusal):
            return done(route.message)
        # Answers arrive only through the connector (ADR-0017): a member with none is refused before any
        # rate event or signed POST.
        connector = registry.connector_refusal(conn, route.registration_id, now)
        if connector:
            return done(connector)
        # A banned secret: a run of this automation called through another member's connector (ADR-0018).
        if foreign.automation_banned(conn, route.automation_id):
            return done(foreign.text(foreign.MSG_BLOCKED, config.membership))
        # One MAC for both the repeat check and the stored row, so they can't drift apart.
        mac = prompt_mac(self._prompt_key, prompt)
        refusal = (
            policy.caller_rate_refusal(conn, caller_id, now, config.per_caller_rpm, config.per_caller_daily)
            or policy.owner_rate_refusal(conn, route.owner_id, now, config.per_owner_rpm, config.per_owner_daily)
            or policy.per_channel_refusal(conn, guild_id, channel_id, caller_id, mac, now,
                                          config.per_channel_max_asks, config.per_channel_posted_seconds)
            or policy.outstanding_refusal(conn, route.registration_id, config.per_owner_max_outstanding)
            or policy.queue_refusal(conn, self._posts_in_flight(),
                                    config.max_concurrent_automation_posts, config.max_queued)
        )
        if refusal:
            return done(refusal)

        # Step 12: accept.
        conn.execute(
            "UPDATE interactions SET status = 'queued', ack_kind = 'defer', private = ?, "
            "guild_id = ?, guild_installed = ?, channel_id = ?, caller_id = ?, "
            "registration_id = ?, owner_id = ?, automation_id = ?, "
            "interaction_token = ?, prompt_mac = ?, correlation_id = ?, updated_at = ? "
            "WHERE interaction_id = ?",
            (int(private), guild_id, int(invocation.guild_installed), channel_id, caller_id,
             route.registration_id, route.owner_id, route.automation_id,
             token, mac, "corr_" + uuid.uuid4().hex, now, interaction_id),
        )
        registry.touch_in_tx(conn, route.registration_id, now)
        conn.executemany(
            "INSERT INTO rate_events (subject_kind, subject_id, at) VALUES (?, ?, ?)",
            [("caller", caller_id, now), ("owner", route.owner_id, now)],
        )
        return AckResult(deferred_response(private), Spawn(interaction_id, prompt))

    def _replay(self, interaction_id: str) -> AckResult:
        row = self._conn.execute(
            "SELECT ack_kind, ack_content, private FROM interactions WHERE interaction_id = ?",
            (interaction_id,),
        ).fetchone()
        if row["ack_kind"] == "defer":
            return AckResult(deferred_response(bool(row["private"])))
        return AckResult(ephemeral_message(row["ack_content"] or MSG_ALREADY_HANDLED))
