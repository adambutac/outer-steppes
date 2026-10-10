"""DESIGN.md, Interaction state machine: every transition T1-T17, and T11a, through real code.

Each case drives one transition with the production function that owns it and
checks the resulting status, whether a final message is pending, whether the
interaction token is kept, and the token-presence invariant. Losing-writer
cases check that a second writer changes nothing.
"""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from outer_steppes import registry
from outer_steppes.automation_client import PostClass, PostOutcome, classify_status
from outer_steppes.db import transaction
from outer_steppes.discord_out import Outcome
from outer_steppes.interactions import InteractionHandler
from outer_steppes.reply import HeldPrompts, ReplyService, mint_reply_token
from outer_steppes.sweeper import Finalizer, expire_replies, finalize, reconcile_startup, sweep_orphans
from outer_steppes.worker import WorkerPool

from .conftest import (
    CALLER, CHANNEL, GUILD, NOW, FakeDiscord, assert_token_invariant, command, connect_member, make_config,
    public_resolver, link_automations, register, snowflake,
)


class Poster:
    def __init__(self, outcome):
        self.outcome, self.calls = outcome, 0

    async def post(self, accepted, body, secret):
        self.calls += 1
        return self.outcome


@pytest.fixture
def config(db_path):
    return make_config(database_path=db_path)


def seed(conn, status, *, post_outcome=None, expires=NOW + 600, chunks=0, created=NOW) -> tuple[str, str]:
    registration_id = "reg-" + CALLER
    if conn.execute("SELECT 1 FROM registrations WHERE id = ?", (registration_id,)).fetchone() is None:
        register(conn)
    token, digest = mint_reply_token()
    iid = snowflake()
    conn.execute(
        "INSERT INTO interactions (interaction_id, status, ack_kind, guild_id, channel_id, caller_id, "
        "registration_id, owner_id, correlation_id, prompt_mac, interaction_token, reply_token_hash, "
        "reply_expires_at, post_outcome, chunks_sent, created_at, updated_at) "
        "VALUES (?, ?, 'defer', ?, ?, ?, ?, ?, 'corr', 'q', 'itok', ?, ?, ?, ?, ?, ?)",
        (iid, status, GUILD, CHANNEL, CALLER, registration_id, CALLER,
         digest if status != "queued" else None, expires, post_outcome, chunks, created, created))
    link_automations(conn)
    return iid, token


def state(conn, iid):
    r = conn.execute("SELECT status, final_message, interaction_token FROM interactions WHERE interaction_id = ?",
                     (iid,)).fetchone()
    return r["status"], r["final_message"] is not None, r["interaction_token"] is not None


async def run_worker(conn, config, iid, poster, resolver=public_resolver):
    finalizer = Finalizer(conn, FakeDiscord(), lambda: NOW)
    pool = WorkerPool(conn, finalizer, config, poster, resolver=resolver, clock=lambda: NOW)
    pool.spawn(iid)
    while iid in pool.live_task_ids():
        await asyncio.sleep(0.005)


ME = "reg-" + CALLER


def replies(conn, config, discord=None, held=None):
    return ReplyService(conn, discord or FakeDiscord(), config, lambda: None, held or HeldPrompts(),
                        clock=lambda: NOW)


# Each case: (transition, async driver(conn, config) -> interaction id, expected (status, final pending, token kept))

async def t1(conn, config):
    interaction = command("frobnicate")
    await InteractionHandler(config, conn, lambda: 0, lambda: NOW).handle(interaction)
    return interaction["id"]


async def t2(conn, config):
    register(conn)
    connect_member(conn)
    interaction = command()
    await InteractionHandler(config, conn, lambda: 0, lambda: NOW).handle(interaction)
    return interaction["id"]


async def t3(conn, config):
    iid, _ = seed(conn, "queued")
    await run_worker(conn, config, iid, Poster(classify_status(202)))
    return iid


async def t4(conn, config):
    async def private(host):
        return ["10.0.0.1"]
    iid, _ = seed(conn, "queued")
    await run_worker(conn, config, iid, Poster(classify_status(202)), resolver=private)
    return iid


async def t5(conn, config):
    iid, _ = seed(conn, "queued")
    with transaction(conn):
        registry.revoke_in_tx(conn, "reg-" + CALLER, NOW)
    return iid


async def t6(conn, config):
    iid, _ = seed(conn, "queued")
    reconcile_startup(conn, NOW)
    return iid


async def t7(conn, config):
    iid, _ = seed(conn, "queued", created=NOW - 1000)
    sweep_orphans(conn, set(), config.queue_wait_seconds, NOW)
    return iid


async def t8(conn, config):
    iid, _ = seed(conn, "queued")
    await run_worker(conn, config, iid, Poster(PostOutcome(PostClass.AMBIGUOUS)))
    return iid


async def t9(conn, config):
    iid, _ = seed(conn, "queued")
    await run_worker(conn, config, iid, Poster(classify_status(429)))
    return iid


async def t10(conn, config):
    iid, _ = seed(conn, "posted", post_outcome="accepted")
    with transaction(conn):
        registry.set_paused_in_tx(conn, True, NOW)
    return iid


async def t11(conn, config):
    iid, token = seed(conn, "posted", post_outcome="accepted")
    conn.execute("UPDATE registrations SET revoked_at = ?", (NOW,))
    await replies(conn, config).deliver_reply(token, "answer", ME)
    return iid


async def t11_fetch(conn, config):
    iid, token = seed(conn, "posted", post_outcome="accepted")
    conn.execute("UPDATE registrations SET revoked_at = ?", (NOW,))
    replies(conn, config).get_prompt(token, ME)
    return iid


async def t11a(conn, config):
    iid, token = seed(conn, "posted", post_outcome="accepted")  # held by an earlier process: nothing held here
    replies(conn, config).get_prompt(token, ME)
    return iid


async def t12(conn, config):
    iid, _ = seed(conn, "posted", post_outcome="accepted", expires=NOW)
    expire_replies(conn, NOW)
    return iid


async def t13(conn, config):
    class Stall(FakeDiscord):
        async def edit_original(self, token, content):
            raise asyncio.CancelledError  # stop mid-delivery to observe 'answering'
    iid, token = seed(conn, "posted", post_outcome="accepted")
    with pytest.raises(asyncio.CancelledError):
        await replies(conn, config, Stall()).deliver_reply(token, "answer", ME)
    return iid


async def t14(conn, config):
    iid, token = seed(conn, "posted", post_outcome="accepted")
    await replies(conn, config).deliver_reply(token, "answer", ME)
    return iid


async def t15(conn, config):
    discord = FakeDiscord()
    discord.outcomes = [Outcome.OK, Outcome.FAILED]
    iid, token = seed(conn, "posted", post_outcome="accepted")
    await replies(conn, config, discord).deliver_reply(token, "word " * 1000, ME)
    return iid


async def t16(conn, config):
    discord = FakeDiscord()
    discord.outcomes = [Outcome.FAILED]
    iid, token = seed(conn, "posted", post_outcome="accepted")
    await replies(conn, config, discord).deliver_reply(token, "answer", ME)
    return iid


async def t17(conn, config):
    unsent, _ = seed(conn, "answering", chunks=0)
    reconcile_startup(conn, NOW)
    return unsent


TRANSITIONS = [
    ("T1  claimed->done (refusal)", t1, ("done", False, False)),
    ("T2  ->queued (accepted /ask)", t2, ("queued", False, True)),
    ("T3  queued->posted (worker)", t3, ("posted", False, True)),
    ("T4  queued->failed (worker: URL rejected)", t4, ("failed", True, True)),
    ("T5  queued->failed (revoke)", t5, ("failed", True, True)),
    ("T6  queued->failed (startup)", t6, ("failed", True, True)),
    ("T7  queued->failed (orphan sweep)", t7, ("failed", True, True)),
    ("T8  posted->posted (ambiguous)", t8, ("posted", False, True)),
    ("T9  posted->failed (not sent)", t9, ("failed", True, True)),
    ("T10 posted->failed (pause)", t10, ("failed", True, True)),
    ("T11 posted->failed (reply withdrawn)", t11, ("failed", True, True)),
    ("T11 posted->failed (fetch withdrawn)", t11_fetch, ("failed", True, True)),
    ("T11a posted->failed (fetch after a restart)", t11a, ("failed", True, True)),
    ("T12 posted->expired (sweeper)", t12, ("expired", True, True)),
    ("T13 posted->answering (claim)", t13, ("answering", False, True)),
    ("T14 answering->answered", t14, ("answered", False, False)),
    ("T15 answering->failed (partial send)", t15, ("failed", False, False)),
    ("T16 answering->failed (nothing sent)", t16, ("failed", True, True)),
    ("T17 answering->failed (startup, nothing sent)", t17, ("failed", True, True)),
]


@pytest.mark.parametrize("name,drive,expected", TRANSITIONS, ids=[t[0].split()[0] + ("-fetch" if "fetch withdrawn" in t[0] else "") for t in TRANSITIONS])
async def test_transition(conn, config, name, drive, expected):
    iid = await drive(conn, config)
    assert state(conn, iid) == expected, name
    if expected[0] != "answering":  # T13 is observed mid-flight on purpose
        assert_token_invariant(conn)


async def test_every_final_message_is_eventually_sent_and_clears_the_token(conn, config):
    """After any finalizing transition, one finalizer pass sends the message and clears the token."""
    for _, drive, expected in TRANSITIONS:
        if expected[1]:
            await drive(conn, config)
    discord = FakeDiscord()
    await Finalizer(conn, discord, lambda: NOW).run_once()
    assert conn.execute("SELECT count(*) FROM interactions WHERE final_message IS NOT NULL").fetchone()[0] == 0
    assert len(discord.calls) >= 10
    assert_token_invariant(conn)


@pytest.mark.parametrize("status", ["posted", "answering", "answered", "failed", "expired"])
def test_losing_writer_changes_nothing(conn, status):
    """A second writer's conditional update loses; the first final message stands."""
    iid, _ = seed(conn, status)
    before = conn.execute("SELECT * FROM interactions WHERE interaction_id = ?", (iid,)).fetchone()
    assert not finalize(conn, iid, ("queued",), "failed", "late writer", NOW)
    after = conn.execute("SELECT * FROM interactions WHERE interaction_id = ?", (iid,)).fetchone()
    assert tuple(before) == tuple(after)


async def test_only_t3_leads_to_a_signature(conn, config):
    """A POST is only ever made from a row the worker itself moved queued->posted."""
    for status in ("posted", "failed", "answering"):
        iid, _ = seed(conn, status)
        poster = Poster(classify_status(202))
        await run_worker(conn, config, iid, poster)
        assert poster.calls == 0, status
