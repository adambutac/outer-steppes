"""finalize(), the finalizer, startup reconciliation, orphans, and the stub worker."""

from __future__ import annotations

import asyncio

import pytest

from outer_steppes.discord_out import Outcome
from outer_steppes.sweeper import (
    MSG_NOT_POSTED, MSG_RESTARTED, Finalizer, finalize, reconcile_startup, snowflake_time,
    sweep_orphans,
)

from .conftest import NOW, snowflake


def insert(conn, *, status="queued", at=NOW, token="tok", final=None, chunks=0, created_at=None) -> str:
    interaction_id = snowflake(at)
    conn.execute(
        "INSERT INTO interactions (interaction_id, status, ack_kind, interaction_token, final_message, "
        "chunks_sent, created_at, updated_at) VALUES (?, ?, 'defer', ?, ?, ?, ?, ?)",
        (interaction_id, status, token, final, chunks, created_at or int(at), int(at)),
    )
    return interaction_id


def row(conn, interaction_id):
    return conn.execute("SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)).fetchone()


async def no_sleep(_seconds):
    return None


def test_snowflake_time_round_trips():
    assert snowflake_time(snowflake(NOW + 12.345)) == pytest.approx(NOW + 12.345, abs=0.001)


# ------------------------------------------------------------------ finalize

def test_finalize_is_conditional_and_keeps_token(conn):
    iid = insert(conn, status="queued")
    assert finalize(conn, iid, ("queued",), "failed", "Busy, try again.", NOW)
    r = row(conn, iid)
    assert (r["status"], r["final_message"], r["interaction_token"]) == ("failed", "Busy, try again.", "tok")

    # A second writer loses: no change, no overwrite of the first message.
    assert not finalize(conn, iid, ("queued",), "failed", "Grok is paused.", NOW)
    assert row(conn, iid)["final_message"] == "Busy, try again."


def test_finalize_accepts_a_status_set(conn):
    iid = insert(conn, status="posted")
    assert finalize(conn, iid, ("queued", "posted"), "failed", "This reply was withdrawn.", NOW)
    assert row(conn, iid)["status"] == "failed"


# ------------------------------------------------------------------ finalizer

async def test_finalizer_sends_then_clears(conn, fake_discord, clock):
    iid = insert(conn, status="failed", final="Busy, try again.")
    cleared = await Finalizer(conn, fake_discord, clock, no_sleep).run_once()
    assert cleared == 1
    assert fake_discord.calls == [("tok", "Busy, try again.")]
    r = row(conn, iid)
    assert (r["final_message"], r["interaction_token"]) == (None, None)


@pytest.mark.parametrize("outcome", [Outcome.UNAUTHORIZED, Outcome.NOT_FOUND])
async def test_finalizer_clears_when_token_is_gone_and_says_so(conn, fake_discord, clock, caplog, outcome):
    iid = insert(conn, status="failed", final="x", at=NOW - 60)
    fake_discord.outcomes = [outcome]
    with caplog.at_level("WARNING"):
        await Finalizer(conn, fake_discord, clock, no_sleep).run_once()
    assert row(conn, iid)["interaction_token"] is None
    assert len(fake_discord.calls) == 1  # 404 long after the ACK is not retried
    assert f"final message not delivered for interaction {iid}" in caplog.text
    assert "tok" not in caplog.text.replace("token", "")  # the token itself is never logged


async def test_finalizer_leaves_row_on_failure_for_next_pass(conn, fake_discord, clock):
    iid = insert(conn, status="failed", final="x")
    fake_discord.outcomes = [Outcome.FAILED]
    finalizer = Finalizer(conn, fake_discord, clock, no_sleep)
    assert await finalizer.run_once() == 0
    assert row(conn, iid)["final_message"] == "x"
    assert await finalizer.run_once() == 1  # next pass succeeds
    assert row(conn, iid)["interaction_token"] is None


async def test_finalizer_retries_404_during_deferral_race(conn, fake_discord, clock):
    iid = insert(conn, status="failed", final="x", at=NOW - 0.5)
    fake_discord.outcomes = [Outcome.NOT_FOUND, Outcome.NOT_FOUND, Outcome.OK]
    await Finalizer(conn, fake_discord, clock, no_sleep).run_once()
    assert len(fake_discord.calls) == 3
    assert row(conn, iid)["interaction_token"] is None


async def test_deferral_race_window_ignores_clock_skew(conn, fake_discord, clock):
    """Regression: a local clock 2+ s ahead of Discord's used to skip the retry.

    The window is measured from our own ACK time (created_at), not from the
    Discord timestamp inside the interaction id.
    """
    interaction_id = snowflake(NOW - 3)  # Discord's clock: created 3 s "ago"
    conn.execute(
        "INSERT INTO interactions (interaction_id, status, ack_kind, interaction_token, final_message, "
        "created_at, updated_at) VALUES (?, 'failed', 'defer', 'tok', 'x', ?, ?)",
        (interaction_id, NOW, NOW),  # our clock: ACKed just now
    )
    fake_discord.outcomes = [Outcome.NOT_FOUND, Outcome.OK]
    await Finalizer(conn, fake_discord, clock, no_sleep).run_once()
    assert len(fake_discord.calls) == 2


async def test_404_after_race_window_is_not_retried(conn, fake_discord, clock):
    insert(conn, status="failed", final="x", at=NOW - 6)
    fake_discord.outcomes = [Outcome.NOT_FOUND]
    await Finalizer(conn, fake_discord, clock, no_sleep).run_once()
    assert len(fake_discord.calls) == 1


async def test_finalizer_clears_old_token_without_calling_discord(conn, fake_discord, clock):
    iid = insert(conn, status="failed", final="x", at=NOW - 891)
    await Finalizer(conn, fake_discord, clock, no_sleep).run_once()
    assert fake_discord.calls == []
    assert row(conn, iid)["final_message"] is None


async def test_finalizer_touches_only_rows_with_pending_messages(conn, fake_discord, clock):
    insert(conn, status="failed", final=None)
    insert(conn, status="answered", final="should never be sent")
    await Finalizer(conn, fake_discord, clock, no_sleep).run_once()
    assert fake_discord.calls == []


async def test_concurrent_finalizer_passes_send_once(conn, clock):
    class SlowDiscord:
        def __init__(self):
            self.calls = 0

        async def edit_original(self, token, content):
            self.calls += 1
            await asyncio.sleep(0.01)
            from outer_steppes.discord_out import Result
            return Result(Outcome.OK)

    insert(conn, status="failed", final="x")
    discord = SlowDiscord()
    finalizer = Finalizer(conn, discord, clock, no_sleep)
    await asyncio.gather(finalizer.run_once(), finalizer.run_once())
    assert discord.calls == 1


# ------------------------------------------------------------------ startup

def test_reconcile_startup(conn):
    queued = insert(conn, status="queued")
    posted = insert(conn, status="posted")
    unsent = insert(conn, status="answering", chunks=0)
    partial = insert(conn, status="answering", chunks=2)
    claimed = insert(conn, status="claimed", token=None)

    counts = reconcile_startup(conn, NOW)
    assert counts == {"queued": 1, "answering_unsent": 1, "answering_partial": 1, "claimed": 1}

    assert (row(conn, queued)["status"], row(conn, queued)["final_message"]) == ("failed", MSG_RESTARTED)
    assert row(conn, queued)["interaction_token"] == "tok"  # kept for the finalizer
    assert row(conn, posted)["status"] == "posted"
    assert (row(conn, unsent)["status"], row(conn, unsent)["final_message"]) == ("failed", MSG_NOT_POSTED)
    assert (row(conn, partial)["status"], row(conn, partial)["final_message"],
            row(conn, partial)["interaction_token"]) == ("failed", None, None)
    assert row(conn, claimed)["status"] == "done"


# ------------------------------------------------------------------ orphans

def test_sweep_orphans_skips_live_tasks_and_young_rows(conn):
    old = insert(conn, status="queued", created_at=NOW - 200)
    live = insert(conn, status="queued", created_at=NOW - 200)
    young = insert(conn, status="queued", created_at=NOW - 100)
    assert sweep_orphans(conn, {live}, queue_wait_seconds=120, now=NOW) == 1
    assert row(conn, old)["final_message"] == "Busy, try again."
    assert row(conn, live)["status"] == "queued"
    assert row(conn, young)["status"] == "queued"


# ------------------------------------------------------------------ expiry (T12)

from outer_steppes.sweeper import MSG_NO_ANSWER, MSG_UNCONFIRMED_NO_ANSWER, expire_replies  # noqa: E402


def test_the_timeout_text_names_what_to_check():
    """Every answer arrives through the connector (ADR-0017), and the bridge can't see it missing from the account
    that owns the trigger, so every accepted request that times out says where to look."""
    assert MSG_NO_ANSWER == ("No answer arrived from Grok in time. Check that **Outer Steppes** is connected in the "
                             "grok.com account that owns your automation, and add or reconnect it there.")


@pytest.mark.parametrize("post_outcome,message", [
    ("accepted", MSG_NO_ANSWER),  # DESIGN.md, Connector status: "The timeout says what to check"
    ("ambiguous", MSG_UNCONFIRMED_NO_ANSWER), ("pending", MSG_UNCONFIRMED_NO_ANSWER),
])
def test_expired_posted_rows_get_the_message_for_their_outcome(conn, post_outcome, message):
    iid = insert(conn, status="posted")
    conn.execute("UPDATE interactions SET reply_expires_at = ?, post_outcome = ? WHERE interaction_id = ?",
                 (NOW, post_outcome, iid))
    assert expire_replies(conn, NOW) == 1
    r = row(conn, iid)
    assert (r["status"], r["final_message"], r["interaction_token"]) == ("expired", message, "tok")


def test_unexpired_and_non_posted_rows_are_left_alone(conn):
    live = insert(conn, status="posted")
    conn.execute("UPDATE interactions SET reply_expires_at = ? WHERE interaction_id = ?", (NOW + 1, live))
    answering = insert(conn, status="answering")
    conn.execute("UPDATE interactions SET reply_expires_at = ? WHERE interaction_id = ?", (NOW - 5, answering))
    assert expire_replies(conn, NOW) == 0


# ------------------------------------------------------------------ the held prompts (DESIGN.md, ACK step 13)

async def test_every_finalizer_pass_drops_ended_prompts_after_its_sends(conn, fake_discord):
    from outer_steppes.sweeper import Finalizer
    order = []

    class Recording(type(fake_discord)):
        async def edit_original(self, token, content):
            order.append("send")
            return await super().edit_original(token, content)

    iid = insert(conn, status="posted")
    finalize(conn, iid, ("posted",), "failed", "This reply was withdrawn.", NOW)
    await Finalizer(conn, Recording(), lambda: NOW, on_pass=lambda: order.append("drop")).run_once()
    assert order == ["send", "drop"]


async def test_a_failing_drop_doesnt_stop_the_final_messages(conn, fake_discord):
    from outer_steppes.sweeper import Finalizer

    def boom():
        raise RuntimeError("db error")

    iid = insert(conn, status="posted")
    finalize(conn, iid, ("posted",), "failed", "This reply was withdrawn.", NOW)
    assert await Finalizer(conn, fake_discord, lambda: NOW, on_pass=boom).run_once() == 1
    assert fake_discord.calls and row(conn, iid)["final_message"] is None
