"""The real worker: slots, DNS, re-check, one signed POST, classification."""

from __future__ import annotations

import asyncio
import json
from dataclasses import replace

import pytest

from outer_steppes import registry
from outer_steppes.automation_client import PostClass, PostOutcome, classify_status
from outer_steppes.db import transaction
from outer_steppes.reply import HeldPrompts, ReplyService, token_hash
from outer_steppes.sweeper import Finalizer
from outer_steppes.worker import WorkerPool

from .conftest import (
    AUTOMATION_URL, CALLER, CHANNEL, GUILD, NOW, SECRET, assert_token_invariant, make_config, public_resolver,
    link_automations, register, snowflake,
)


class FakePoster:
    def __init__(self, outcome=None, hook=None):
        self.calls = []
        self.outcome = outcome or classify_status(202)
        self.hook = hook  # awaited with (body, secret) before returning

    async def post(self, accepted, body, secret):
        self.accepted = getattr(self, "accepted", []) + [accepted]
        self.calls.append({"url": accepted.url, "body": json.loads(body), "secret": secret,
                           "addresses": accepted.addresses})
        if self.hook:
            await self.hook(body, secret)
        return self.outcome


class Waker:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1


@pytest.fixture
def config(db_path):
    return make_config(database_path=db_path)


def queue(conn, *, registration_id=None, prompt_mac="hello", private=0) -> str:
    registration_id = registration_id or register(conn)
    iid = snowflake()
    conn.execute(
        "INSERT INTO interactions (interaction_id, status, ack_kind, private, guild_id, channel_id, caller_id, "
        "registration_id, owner_id, correlation_id, prompt_mac, "
        "interaction_token, created_at, updated_at) "
        "VALUES (?, 'queued', 'defer', ?, ?, ?, ?, ?, ?, 'corr_x', ?, 'itok', ?, ?)",
        (iid, private, GUILD, CHANNEL, CALLER, registration_id, CALLER, prompt_mac, NOW, NOW))
    link_automations(conn)
    return iid


def row(conn, iid):
    return conn.execute("SELECT * FROM interactions WHERE interaction_id = ?", (iid,)).fetchone()


def make_pool(conn, config, poster, resolver=public_resolver, clock=lambda: NOW):
    finalizer = Finalizer(conn, fake_discord_stub(), clock)
    finalizer.wake = Waker()
    return WorkerPool(conn, finalizer, config, poster, resolver=resolver, bind_hosts={"127.0.0.1"},
                      clock=clock), finalizer


def fake_discord_stub():
    from .conftest import FakeDiscord
    return FakeDiscord()


async def run_to_completion(pool, iid):
    pool.spawn(iid)
    for _ in range(200):
        if iid not in pool.live_task_ids():
            return
        await asyncio.sleep(0.005)
    raise AssertionError("worker did not finish")


# ------------------------------------------------------------------ happy path

async def test_posts_once_with_exact_payload_and_stores_only_a_token_hash(conn, config):
    iid = queue(conn)
    poster = FakePoster()
    pool, _ = make_pool(conn, config, poster)
    await run_to_completion(pool, iid)

    [call] = poster.calls
    body = call["body"]
    # No prompt, no Discord ids and no reply_url: the run fetches them through the member's connector (ADR-0017).
    assert list(body) == ["instructions", "correlation_id", "reply_token", "reply_expires_at"]
    assert body["correlation_id"] == "corr_x"
    assert body["reply_token"].startswith("rt_") and len(body["reply_token"]) == 46
    assert call["secret"] == SECRET and call["url"] == AUTOMATION_URL

    r = row(conn, iid)
    assert (r["status"], r["post_outcome"]) == ("posted", "accepted")
    assert r["reply_token_hash"] == token_hash(body["reply_token"])
    assert body["reply_token"] not in str(tuple(r))       # only the hash is stored
    assert r["reply_expires_at"] == body["reply_expires_at"]
    assert r["posted_at"] == NOW
    assert pool.posts_in_flight == 0
    assert_token_invariant(conn)


async def test_reply_expiry_is_anchored_on_the_interaction_id(conn, config):
    iid = queue(conn)
    pool, _ = make_pool(conn, replace(config, reply_ttl_seconds=600), FakePoster())
    await run_to_completion(pool, iid)
    from outer_steppes.sweeper import snowflake_time
    assert row(conn, iid)["reply_expires_at"] == int(snowflake_time(iid)) + 600


# ------------------------------------------------------------------ classification

async def test_ambiguous_outcome_stays_posted(conn, config):
    iid = queue(conn)
    pool, finalizer = make_pool(conn, config, FakePoster(PostOutcome(PostClass.AMBIGUOUS)))
    await run_to_completion(pool, iid)
    assert (row(conn, iid)["status"], row(conn, iid)["post_outcome"]) == ("posted", "ambiguous")
    assert row(conn, iid)["final_message"] is None  # no interim PATCH
    assert finalizer.wake.count == 0


@pytest.mark.parametrize("status,message", [
    (429, "Grok is busy. Try again in a minute."), (503, "Grok is busy. Try again in a minute."),
    (401, "The automation rejected the signature."), (400, "The automation did not accept the request."),
])
async def test_not_sent_fails_the_row_and_kills_the_token(conn, config, status, message):
    iid = queue(conn)
    pool, finalizer = make_pool(conn, config, FakePoster(classify_status(status)))
    await run_to_completion(pool, iid)
    r = row(conn, iid)
    assert (r["status"], r["final_message"]) == ("failed", message)
    assert finalizer.wake.count == 1
    assert_token_invariant(conn)


# ------------------------------------------------------------------ re-checks before sending

async def test_rejected_url_fails_without_post(conn, config):
    async def private(host):
        return ["10.0.0.1"]
    iid = queue(conn)
    poster = FakePoster()
    pool, _ = make_pool(conn, config, poster, resolver=private)
    await run_to_completion(pool, iid)
    assert poster.calls == []
    assert row(conn, iid)["final_message"] == "That automation URL was rejected."


async def test_revoke_during_dns_means_no_post(conn, config):
    registration_id = register(conn)
    iid = queue(conn, registration_id=registration_id)

    async def revoking_resolver(host):
        with transaction(conn):
            registry.revoke_in_tx(conn, registration_id, NOW)
        return await public_resolver(host)

    poster = FakePoster()
    pool, _ = make_pool(conn, config, poster, resolver=revoking_resolver)
    await run_to_completion(pool, iid)
    assert poster.calls == []
    assert row(conn, iid)["final_message"] == "This reply was withdrawn."  # the revoke owns the final message


async def test_automation_replaced_after_dns_means_no_post(conn, config):
    """Replacing the automation withdraws what was accepted for the old one (PR 8b)."""
    registration_id = register(conn)
    iid = queue(conn, registration_id=registration_id)

    async def replacing_resolver(host):
        with transaction(conn):
            registry.set_automation_in_tx(conn, registration_id, AUTOMATION_URL, SECRET, NOW)  # identical values
        return await public_resolver(host)

    poster = FakePoster()
    pool, _ = make_pool(conn, config, poster, resolver=replacing_resolver)
    await run_to_completion(pool, iid)
    assert poster.calls == []
    assert (row(conn, iid)["status"], row(conn, iid)["final_message"]) == ("failed", "This reply was withdrawn.")


async def test_automation_removed_after_dns_means_no_post(conn, config):
    registration_id = register(conn)
    iid = queue(conn, registration_id=registration_id)

    async def removing_resolver(host):
        with transaction(conn):
            registry.remove_automation_in_tx(conn, registration_id, NOW)
        return await public_resolver(host)

    poster = FakePoster()
    pool, _ = make_pool(conn, config, poster, resolver=removing_resolver)
    await run_to_completion(pool, iid)
    assert poster.calls == []
    assert (row(conn, iid)["status"], row(conn, iid)["final_message"]) == ("failed", "This reply was withdrawn.")


async def test_url_changed_in_place_after_dns_means_no_post(conn, config):
    """Defence in depth: automations are never updated in place, but if one were, the worker still won't POST."""
    registration_id = register(conn)
    iid = queue(conn, registration_id=registration_id)

    async def swapping_resolver(host):
        conn.execute("UPDATE automations SET automation_url = ? WHERE registration_id = ?",
                     (AUTOMATION_URL.replace("000000000001", "000000000002"), registration_id))
        return await public_resolver(host)

    poster = FakePoster()
    pool, _ = make_pool(conn, config, poster, resolver=swapping_resolver)
    await run_to_completion(pool, iid)
    assert poster.calls == []
    assert (row(conn, iid)["status"], row(conn, iid)["final_message"]) == ("failed", "Not authorized.")


async def test_pause_during_dns_means_no_post(conn, config):
    iid = queue(conn)

    async def pausing_resolver(host):
        with transaction(conn):
            registry.set_paused_in_tx(conn, True, NOW)
        return await public_resolver(host)

    poster = FakePoster()
    pool, _ = make_pool(conn, config, poster, resolver=pausing_resolver)
    await run_to_completion(pool, iid)
    assert poster.calls == []
    assert row(conn, iid)["final_message"] == "Grok is paused. This request was withdrawn."


async def test_worker_skips_dns_if_row_already_finalized(conn, config):
    iid = queue(conn)
    conn.execute("UPDATE interactions SET status = 'failed', final_message = 'x' WHERE interaction_id = ?", (iid,))
    looked_up = []

    async def resolver(host):
        looked_up.append(host)
        return await public_resolver(host)

    poster = FakePoster()
    pool, _ = make_pool(conn, config, poster, resolver=resolver)
    await run_to_completion(pool, iid)
    assert looked_up == [] and poster.calls == []
    assert row(conn, iid)["final_message"] == "x"


# ------------------------------------------------------------------ races and slots

async def test_reply_arriving_before_the_202_is_handled_is_delivered(conn, config, fake_discord):
    iid = queue(conn)
    replies = ReplyService(conn, fake_discord, config, lambda: None, HeldPrompts(), clock=lambda: NOW)
    results = []

    async def fast_automation(body, secret):
        results.append(await replies.deliver_reply(json.loads(body)["reply_token"], "the answer", "reg-" + CALLER))

    pool, _ = make_pool(conn, config, FakePoster(hook=fast_automation))
    await run_to_completion(pool, iid)
    assert results == ["delivered"]
    r = row(conn, iid)
    assert r["status"] == "answered"
    assert r["post_outcome"] == "pending"  # the late classification update changed nothing
    assert_token_invariant(conn)


async def test_queued_request_waits_for_a_slot_and_fails_only_at_queue_wait(conn, config):
    release = asyncio.Event()

    async def blocked(body, secret):
        await release.wait()

    cfg = replace(config, max_concurrent_automation_posts=1, queue_wait_seconds=0.3,
                  automation_timeout_seconds=0.05)
    first, second = queue(conn), queue(conn, registration_id="reg-" + CALLER)
    pool, _ = make_pool(conn, cfg, FakePoster(hook=blocked))
    pool.spawn(first)
    pool.spawn(second)
    await asyncio.sleep(0.1)  # past the automation timeout: still queued, not failed
    assert pool.posts_in_flight == 1
    assert row(conn, second)["status"] == "queued"
    await asyncio.sleep(0.35)  # past QUEUE_WAIT_SECONDS
    assert (row(conn, second)["status"], row(conn, second)["final_message"]) == ("failed", "Busy, try again.")
    release.set()
    await asyncio.sleep(0.05)
    assert row(conn, first)["status"] == "posted"
    await pool.close()


async def test_secret_and_token_are_not_logged(conn, config, caplog):
    iid = queue(conn)
    poster = FakePoster()
    pool, _ = make_pool(conn, config, poster)
    with caplog.at_level("DEBUG"):
        await run_to_completion(pool, iid)
    token = poster.calls[0]["body"]["reply_token"]
    for record in caplog.records:
        text = record.getMessage() + str(record.__dict__)
        assert token not in text and SECRET not in text and "a private question" not in text


# ------------------------------------------------------------------ Grok Bot (ADR-0020)

async def test_a_grok_bot_automation_is_posted_with_its_key_for_a_bearer(conn, config):
    import hashlib
    key = "crsr_" + hashlib.sha256(b"made-up grok bot key").hexdigest()  # made up
    registration_id = register(conn, url="https://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0", secret=key)
    conn.execute("UPDATE automations SET kind = 'grok_bot_webhook' WHERE registration_id = ?", (registration_id,))
    iid = queue(conn, registration_id=registration_id)
    poster = FakePoster()
    pool, _ = make_pool(conn, config, poster)
    await run_to_completion(pool, iid)
    [call] = poster.calls
    assert (call["url"], call["secret"]) == ("https://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0", key)
    assert pool_accepted_auth(poster) == "bearer"


def pool_accepted_auth(poster):
    return poster.accepted[-1].auth
