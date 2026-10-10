"""Reply rendering, chunking, the held prompts, get_prompt and deliver_reply."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from outer_steppes import foreign, metrics
from outer_steppes import reply as reply_mod
from outer_steppes.discord_out import Outcome
from outer_steppes.reply import (
    ATTRIBUTION, MAX_CHUNKS, MESSAGE_LIMIT, HeldPrompts, ReplyService, attribute, chunk, mint_reply_token, render,
)

from .conftest import (
    CALLER, GUILD, NOW, SECRET, assert_token_invariant, link_automations, make_config, register, snowflake,
)

ME = "reg-" + CALLER
OTHER_USER = "222222222222222222"
OTHER = "reg-" + OTHER_USER
RESTARTED_TEXT = "The bridge restarted before Grok could read your message. Please ask again."


# ------------------------------------------------------------------ rendering and chunking

def test_render_quotes_one_line_then_the_answer():
    """No line naming whose automation answered: every answer comes from the caller's own (ADR-0009)."""
    assert render("what is up", "Answer.") == "> what is up\nAnswer."
    assert render("what\n  is\tup", "A") == "> what is up\nA"  # whitespace collapsed: one line
    assert render("x" * 200, "A") == "> " + "x" * 200 + "\nA"  # exactly 200: nothing cut, no ellipsis
    assert render("x" * 201, "A") == "> " + "x" * 200 + "…\nA"


def test_with_no_prompt_held_the_answer_has_no_quote():
    """An answer from a run that fetched before a restart (DESIGN.md, Rendering and chunking)."""
    assert render(None, "Answer.") == "Answer."


def test_chunks_respect_limits():
    text = render("q" * 250, ("word " * 1300)[:7000])
    chunks = chunk(text)
    assert 1 < len(chunks) <= 5
    assert all(len(c) <= 2000 for c in chunks)
    assert all(len(c) > 1800 for c in chunks[:-1])
    assert [w for c in chunks for w in c.split()] == text.split()  # nothing lost, no word split


def test_long_word_is_hard_split():
    chunks = chunk("a" * 3000)
    assert [len(c) for c in chunks] == [2000, 1000]


def test_worst_case_fits_in_five_messages():
    header = render("q" * 250, "")
    for content in ("x" * 7000, ("y" * 199 + " ") * 35, "\n\n".join(["z" * 150] * 46)[:7000]):
        chunks = chunk(header + content[:7000])
        assert len(chunks) <= 5, len(chunks)


def test_the_attribution_ends_the_last_message_after_a_blank_line():
    """xAI's terms require crediting Grok (DESIGN.md, Rendering and chunking)."""
    assert ATTRIBUTION == "Written with Grok"
    assert attribute(["> q\nAnswer."]) == ["> q\nAnswer.\n\nWritten with Grok"]
    assert attribute(["one", "Answer. \n\n"]) == ["one", "Answer.\n\nWritten with Grok"]  # trailing space dropped


def test_the_attribution_gets_its_own_message_only_when_it_does_not_fit():
    fits = "a" * (MESSAGE_LIMIT - len("\n\n" + ATTRIBUTION))
    assert attribute([fits]) == [fits + "\n\n" + ATTRIBUTION]
    assert attribute([fits + "a"]) == [fits + "a", ATTRIBUTION]


def test_worst_case_with_the_attribution_fits_in_five_messages():
    """The attribution needs its own message only when it doesn't fit on the last one, which happens only to answers
    of at most three messages, so an answer still fits in five."""
    header = render("q" * 250, "")
    contents = ["x" * 7000, ("y" * 199 + " ") * 35, "\n\n".join(["z" * 150] * 46)[:7000]]
    # Answers whose last message is too full for the attribution, at every message count that allows it (1-3;
    # three over 1800 plus a fourth over 1981 is more than REPLY_MAX_CHARS plus the header).
    contents += ["w" * (2000 * n - len(header) - k) for n in range(1, 4) for k in range(0, 20)]
    texts = [header + content[:7000] for content in contents]
    # Without the quote (after a restart), answers of 1-3 messages whose last is 1990 characters.
    texts += [render(None, "w" * (2000 * m + 1990)) for m in range(3)]
    own_message = 0
    for text in texts:
        messages = attribute(chunk(text))
        own_message += messages[-1] == ATTRIBUTION
        assert len(messages) <= MAX_CHUNKS, len(messages)
        assert all(len(m) <= MESSAGE_LIMIT for m in messages)
        assert "".join(messages).count(ATTRIBUTION) == 1 and messages[-1].endswith(ATTRIBUTION)
    assert own_message >= 3  # the overflow path really ran, including after full messages


def test_no_whitespace_only_chunks():
    assert chunk("   \n\n  ") == []
    assert all(c.strip() for c in chunk(("a" * 1990) + " " * 500 + "b"))


def test_minted_tokens():
    token, digest = mint_reply_token()
    assert token.startswith("rt_") and len(token) == 46
    assert digest == reply_mod.token_hash(token) and token not in digest


# ------------------------------------------------------------------ fixtures

class Waker:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1


@pytest.fixture
def config(db_path):
    return make_config(database_path=db_path)


@pytest.fixture
def waker():
    return Waker()


@pytest.fixture
def held():
    return HeldPrompts()


@pytest.fixture
def service(conn, fake_discord, config, waker, held):
    return ReplyService(conn, fake_discord, config, waker, held, clock=lambda: NOW)


def posted(conn, held=None, *, status="posted", expires=NOW + 600, private=0, owner=CALLER,
           registration_id=None, prompt="the question") -> tuple[str, str]:
    """A row the worker has posted. With `held`, its prompt is held in memory, as the spawn does."""
    registration_id = registration_id or register(conn)
    token, digest = mint_reply_token()
    iid = snowflake()
    conn.execute(
        "INSERT INTO interactions (interaction_id, status, ack_kind, private, guild_id, channel_id, caller_id, "
        "registration_id, owner_id, prompt_mac, interaction_token, "
        "reply_token_hash, reply_expires_at, post_outcome, created_at, updated_at) "
        "VALUES (?, ?, 'defer', ?, ?, 'c', ?, ?, ?, 'mac', 'itok', ?, ?, 'accepted', ?, ?)",
        (iid, status, private, GUILD, CALLER, registration_id, owner, digest, expires, NOW, NOW))
    link_automations(conn)
    if held is not None:
        held.hold(iid, prompt)
    return iid, token


def row(conn, iid):
    return conn.execute("SELECT * FROM interactions WHERE interaction_id = ?", (iid,)).fetchone()


# ------------------------------------------------------------------ get_prompt

def test_get_prompt_returns_the_prompt_caller_and_channel_without_consuming(service, conn, held):
    iid, token = posted(conn, held, prompt="what's in my inbox?")
    result, text = service.get_prompt(token, ME)
    assert result == "ok"
    assert text == '{"prompt":"what\'s in my inbox?","discord_user_id":"%s","channel_id":"c"}' % CALLER  # compact
    assert service.get_prompt(token, ME) == (result, text)  # fetched again until the interaction ends
    assert row(conn, iid)["status"] == "posted"


def test_get_prompt_answers_only_the_asking_members_connector(service, conn, held, waker):
    """ADR-0017: a run without the member's connector never gets the prompt. A token with no row writes nothing."""
    iid, token = posted(conn, held, prompt="private")
    assert service.get_prompt("rt_nope", ME) == ("unknown", None)
    assert row(conn, iid)["status"] == "posted" and waker.count == 0
    assert service.get_prompt(token, OTHER) == ("unknown", None)  # the foreign ending (T11b): still plain unknown
    assert row(conn, iid)["status"] == "failed"


@pytest.mark.parametrize("token", [None, "", 7])
def test_get_prompt_needs_a_token(service, token):
    assert service.get_prompt(token, ME) == ("bad_request", None)


@pytest.mark.parametrize("status", ["queued", "answering", "answered", "failed", "expired"])
def test_get_prompt_is_closed_for_any_status_but_posted(service, conn, held, status):
    iid, token = posted(conn, held, status=status)
    assert service.get_prompt(token, ME) == ("closed", None)
    assert row(conn, iid)["status"] == status


def test_get_prompt_is_closed_once_the_reply_window_has_closed(service, conn, held):
    iid, token = posted(conn, held, expires=NOW)
    assert service.get_prompt(token, ME) == ("closed", None)
    assert row(conn, iid)["status"] == "posted"  # the sweeper does the expiry


def test_get_prompt_runs_the_recheck_and_withdraws(service, conn, held, waker, fake_discord):
    iid, token = posted(conn, held)
    conn.execute("UPDATE registrations SET revoked_at = ? WHERE id = ?", (NOW, ME))
    assert service.get_prompt(token, ME) == ("revoked", None)  # T11
    r = row(conn, iid)
    assert (r["status"], r["final_message"]) == ("failed", "This reply was withdrawn.")
    assert waker.count == 1 and fake_discord.calls == []
    assert service.get_prompt(token, ME) == ("closed", None)


def test_after_a_restart_a_fetch_ends_the_ask_with_the_ask_again_text(service, conn, waker):
    """T11a: the prompt was held by an earlier process. Nobody is left on "thinking…"."""
    iid, token = posted(conn)  # nothing held: this process never saw the /ask
    assert service.get_prompt(token, ME) == ("restarted", None)
    r = row(conn, iid)
    assert (r["status"], r["final_message"], r["interaction_token"]) == ("failed", RESTARTED_TEXT, "itok")
    assert waker.count == 1
    assert service.get_prompt(token, ME) == ("closed", None)
    assert_token_invariant(conn)


def test_a_withdrawal_after_a_restart_is_reported_as_a_withdrawal(service, conn):
    iid, token = posted(conn)
    conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    assert service.get_prompt(token, ME) == ("revoked", None)
    assert row(conn, iid)["final_message"] == "This reply was withdrawn."


def test_every_fetch_is_counted_and_logged_without_token_or_prompt(service, conn, held, caplog):
    metrics.reset()
    iid, token = posted(conn, held, prompt="the secret question")
    with caplog.at_level("INFO"):
        service.get_prompt(token, ME)
        service.get_prompt("rt_nope", ME)
    lines = [r.getMessage() for r in caplog.records if r.name == "outer_steppes.reply"]
    assert lines == [f"prompt fetch: ok (interaction {iid})", "prompt fetch: unknown (interaction unknown)"]
    assert token not in caplog.text and "the secret question" not in caplog.text
    text = metrics.METRICS.render()
    assert 'bridge_prompt_fetch_total{result="ok"} 1' in text
    assert 'bridge_prompt_fetch_total{result="unknown"} 1' in text


# ------------------------------------------------------------------ the held prompts

def test_held_prompts_are_dropped_only_once_the_interaction_has_ended(conn, held):
    live, _ = posted(conn, held)
    ended, _ = posted(conn, held, registration_id=ME)
    queued, _ = posted(conn, held, registration_id=ME, status="queued")
    conn.execute("UPDATE interactions SET status = 'failed' WHERE interaction_id = ?", (ended,))
    held.drop_ended(conn)
    assert (held.get(live), held.get(ended), held.get(queued)) == ("the question", None, "the question")


async def test_a_delivered_answer_drops_its_prompt_after_the_commit(service, conn, held):
    iid, token = posted(conn, held)
    assert await service.deliver_reply(token, "x", ME) == "delivered"
    assert held.get(iid) is None and len(held) == 0


@pytest.mark.parametrize("outcomes", [[Outcome.FAILED], [Outcome.OK, Outcome.FAILED]])
async def test_a_failed_delivery_drops_its_prompt(service, conn, held, fake_discord, outcomes):
    iid, token = posted(conn, held)
    fake_discord.outcomes = outcomes
    assert await service.deliver_reply(token, "word " * 1000, ME) == "discord_error"
    assert held.get(iid) is None


# ------------------------------------------------------------------ deliver_reply

async def test_delivered_single_message(service, conn, held, fake_discord):
    iid, token = posted(conn, held)
    assert await service.deliver_reply(token, "Here you go <@1> @everyone", ME) == "delivered"
    assert fake_discord.calls == [("itok", "> the question\nHere you go <@1> @everyone\n\nWritten with Grok")]
    r = row(conn, iid)
    assert (r["status"], r["interaction_token"], r["chunks_sent"]) == ("answered", None, 1)
    assert_token_invariant(conn)


async def test_an_answer_after_a_restart_is_posted_without_the_quote(service, conn, fake_discord):
    """The run fetched before the restart; the prompt is gone, the answer isn't."""
    iid, token = posted(conn)
    assert await service.deliver_reply(token, "The answer.", ME) == "delivered"
    assert fake_discord.calls == [("itok", "The answer.\n\nWritten with Grok")]


async def test_deliver_reply_does_not_need_an_earlier_fetch(service, conn, held, fake_discord):
    """DESIGN.md, deliver_reply: only the member's own connector can post, so a fetched flag isn't required."""
    _, token = posted(conn, held)
    assert await service.deliver_reply(token, "x", ME) == "delivered"


async def test_multi_chunk_private_uses_ephemeral_followups(service, conn, held, fake_discord):
    iid, token = posted(conn, held, private=1)
    assert await service.deliver_reply(token, "word " * 1000, ME) == "delivered"
    assert len(fake_discord.calls) == 1 and len(fake_discord.followups) >= 2
    assert all(ephemeral for _, _, ephemeral in fake_discord.followups)
    assert row(conn, iid)["chunks_sent"] == 1 + len(fake_discord.followups)
    sent = [fake_discord.calls[0][1]] + [content for _, content, _ in fake_discord.followups]
    assert "".join(sent).count(ATTRIBUTION) == 1 and sent[-1].endswith("\n\n" + ATTRIBUTION)


async def test_token_is_single_use(service, conn, held):
    iid, token = posted(conn, held)
    assert await service.deliver_reply(token, "one", ME) == "delivered"
    assert await service.deliver_reply(token, "two", ME) == "used"


@pytest.mark.parametrize("status", ["failed", "expired"])
async def test_closed_rows(service, conn, status):
    _, token = posted(conn, status=status)
    assert await service.deliver_reply(token, "x", ME) == "closed"


async def test_expired_by_time_is_closed_without_writing(service, conn):
    iid, token = posted(conn, expires=NOW)
    assert await service.deliver_reply(token, "x", ME) == "closed"
    assert row(conn, iid)["status"] == "posted"  # the sweeper does the expiry


async def test_unknown_token_and_another_members_connector(service, conn, held, fake_discord):
    """A reply token alone posts nothing: the caller's membership must be the row's (ADR-0017). Another member's
    connector ends the live ask instead of answering it (T11b)."""
    iid, token = posted(conn, held)
    assert await service.deliver_reply("rt_nope", "x", ME) == "unknown"
    assert row(conn, iid)["status"] == "posted"  # nothing consumed
    assert await service.deliver_reply(token, "x", OTHER) == "unknown"
    assert row(conn, iid)["status"] == "failed" and fake_discord.calls == []
    assert await service.deliver_reply(token, "x", ME) == "closed"


@pytest.mark.parametrize("content,expected", [("", "bad_request"), ("   ", "bad_request"), (None, "bad_request"),
                                              ("x" * 7001, "too_long")])
async def test_invalid_content_does_not_consume_the_token(service, conn, content, expected):
    iid, token = posted(conn)
    assert await service.deliver_reply(token, content, ME) == expected
    assert row(conn, iid)["status"] == "posted"


async def test_revoked_registration_withdraws_the_reply(service, conn, fake_discord, waker):
    iid, token = posted(conn)
    conn.execute("UPDATE registrations SET revoked_at = ? WHERE discord_user_id = ?", (NOW, CALLER))
    assert await service.deliver_reply(token, "x", ME) == "revoked"
    r = row(conn, iid)
    assert (r["status"], r["final_message"]) == ("failed", "This reply was withdrawn.")
    assert waker.count == 1 and fake_discord.calls == []
    assert await service.deliver_reply(token, "x", ME) == "closed"


async def test_pause_withdraws_the_reply(service, conn):
    _, token = posted(conn)
    conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    assert await service.deliver_reply(token, "x", ME) == "revoked"


async def test_replaced_automation_withdraws_the_reply(service, conn, fake_discord, waker):
    """PR 8b: the re-check requires the automation the /ask was accepted for to still be active."""
    iid, token = posted(conn)
    conn.execute("UPDATE automations SET removed_at = ? WHERE registration_id = ?", (NOW, ME))
    conn.execute("INSERT INTO automations (id, registration_id, kind, automation_url, signing_secret, created_at) "
                 "VALUES ('newer', ?, 'grok_webhook', 'u', 's', ?)", (ME, NOW))
    assert await service.deliver_reply(token, "x", ME) == "revoked"
    assert (row(conn, iid)["status"], row(conn, iid)["final_message"]) == ("failed", "This reply was withdrawn.")
    assert fake_discord.calls == []


async def test_discord_failure_on_chunk_2_leaves_chunk_1(service, conn, fake_discord, waker):
    iid, token = posted(conn)
    fake_discord.outcomes = [Outcome.OK, Outcome.FAILED]
    assert await service.deliver_reply(token, "word " * 1000, ME) == "discord_error"
    r = row(conn, iid)
    assert (r["status"], r["interaction_token"], r["final_message"], r["chunks_sent"]) == ("failed", None, None, 1)
    assert waker.count == 0
    assert await service.deliver_reply(token, "again", ME) == "closed"
    assert_token_invariant(conn)


async def test_discord_failure_on_chunk_1_queues_a_final_message(service, conn, fake_discord, waker):
    iid, token = posted(conn)
    fake_discord.outcomes = [Outcome.FAILED]
    assert await service.deliver_reply(token, "x", ME) == "discord_error"
    r = row(conn, iid)
    assert (r["status"], r["final_message"], r["interaction_token"]) == (
        "failed", "The answer couldn't be posted to Discord.", "itok")
    assert waker.count == 1
    assert_token_invariant(conn)


async def test_every_attempt_is_logged_and_counted_without_token_or_content(service, conn, caplog):
    metrics.reset()
    iid, token = posted(conn)
    with caplog.at_level("INFO"):
        assert await service.deliver_reply(token, "x" * 7001, ME) == "too_long"
        assert await service.deliver_reply(token, "the secret answer", ME) == "delivered"
        assert await service.deliver_reply("rt_nope", "x", ME) == "unknown"
    lines = [r.getMessage() for r in caplog.records if r.name == "outer_steppes.reply"]
    assert lines == [
        f"reply: too_long (interaction {iid}, 7001 chars)",
        f"reply: delivered (interaction {iid}, 17 chars)",
        "reply: unknown (interaction unknown, 1 chars)",
    ]
    assert token not in caplog.text and "the secret answer" not in caplog.text
    assert 'bridge_reply_total{result="delivered"} 1' in metrics.METRICS.render()  # no source label


async def test_a_delivered_answer_renews_the_idle_clock_and_a_refused_one_does_not(service, conn):
    """PR 8d-1."""
    iid, token = posted(conn)
    conn.execute("UPDATE registrations SET last_used_at = 1")
    last_used = lambda: conn.execute("SELECT last_used_at FROM registrations").fetchone()[0]  # noqa: E731
    assert await service.deliver_reply("rt_nope", "x", ME) == "unknown"
    assert await service.deliver_reply(token, "", ME) == "bad_request"
    assert last_used() == 1
    assert await service.deliver_reply(token, "an answer", ME) == "delivered"
    assert last_used() == NOW


def test_the_fetched_json_parses_back(service, conn, held):
    _, token = posted(conn, held, prompt='quotes " and \\ and ünïcode\nnewline')
    _, text = service.get_prompt(token, ME)
    assert json.loads(text)["prompt"] == 'quotes " and \\ and ünïcode\nnewline'


# ------------------------------------------------------------------ another member's connector (ADR-0018)

def banned(conn) -> set[str]:
    return {r[0] for r in conn.execute("SELECT secret_hash FROM banned_secrets")}


def foreign_use_at(conn, registration_id):
    return conn.execute("SELECT foreign_use_at FROM registrations WHERE id = ?", (registration_id,)).fetchone()[0]


OTHER_SECRET = "whsec_" + base64.b64encode(b"fedcba9876543210").decode()  # made up
ENDED = foreign.text(foreign.MSG_FOREIGN_ENDED, make_config().membership)


@pytest.fixture
def other(conn):
    """Another member, whose connector makes the call. Metrics start from zero."""
    metrics.reset()
    return register(conn, OTHER_USER, secret=OTHER_SECRET)


def test_get_prompt_through_another_members_connector_ends_the_ask_and_bans_the_token_owners_secret(
        service, conn, held, waker, other):
    iid, token = posted(conn, held, prompt="private")
    assert service.get_prompt(token, other) == ("unknown", None)  # no prompt, and nothing new for the caller
    r = row(conn, iid)
    assert r["status"] == "failed" and r["final_message"] == ENDED
    assert banned(conn) == {foreign.secret_hash(SECRET)}  # the token owner's secret
    assert foreign_use_at(conn, other) == NOW and foreign_use_at(conn, ME) is None  # the caller is only warned
    assert waker.count == 1
    assert foreign.automation_banned(conn, ME) and not foreign.automation_banned(conn, other)
    assert metrics.METRICS.prompt_fetches.value("unknown") == 1
    assert metrics.METRICS.foreign_connector.value("get_prompt") == 1


async def test_discord_reply_through_another_members_connector_does_the_same(service, conn, held, waker,
                                                                            fake_discord, other):
    """A run that skips the fetch is the same evidence (DESIGN.md, deliver_reply)."""
    iid, token = posted(conn, held)
    assert await service.deliver_reply(token, "an answer", other) == "unknown"
    r = row(conn, iid)
    assert r["status"] == "failed" and r["final_message"] == ENDED
    assert banned(conn) == {foreign.secret_hash(SECRET)} and foreign_use_at(conn, other) == NOW
    assert fake_discord.calls == [] and waker.count == 1
    assert metrics.METRICS.replies.value("unknown") == 1
    assert metrics.METRICS.foreign_connector.value("discord_reply") == 1


def test_the_foreign_check_comes_before_the_recheck_and_the_restart_ending(service, conn, held, other):
    """The mismatch is evidence of the setup either way: a server dropped from GUILD_ALLOWLIST, or a lost prompt."""
    iid, token = posted(conn, held)
    held.drop(iid)
    conn.execute("UPDATE interactions SET guild_id = 'not-allowed' WHERE interaction_id = ?", (iid,))
    assert service.get_prompt(token, other) == ("unknown", None)
    assert row(conn, iid)["final_message"] == ENDED
    assert banned(conn)


@pytest.mark.parametrize("status,expires", [("answering", NOW + 600), ("answered", NOW + 600),
                                            ("failed", NOW + 600), ("expired", NOW + 600), ("posted", NOW)])
async def test_only_a_live_posted_ask_takes_the_foreign_ending(service, conn, held, waker, other, status, expires):
    """An ended row's token is no longer the live run's, and an answering row is claimed by its owner's connector."""
    iid, token = posted(conn, held, status=status, expires=expires)
    assert service.get_prompt(token, other) == ("unknown", None)
    assert await service.deliver_reply(token, "x", other) == "unknown"
    assert row(conn, iid)["status"] == status
    assert banned(conn) == set() and foreign_use_at(conn, other) is None and waker.count == 0
    assert metrics.METRICS.foreign_connector.value("get_prompt") == 0


@pytest.mark.parametrize("content,expected", [("", "bad_request"), ("x" * 7001, "too_long")])
async def test_bad_content_through_another_members_connector_writes_nothing(service, conn, other, content,
                                                                            expected):
    """deliver_reply checks the content first (step 1); accepted in DESIGN.md."""
    iid, token = posted(conn)
    assert await service.deliver_reply(token, content, other) == expected
    assert row(conn, iid)["status"] == "posted" and banned(conn) == set()


def test_a_removed_automation_is_not_banned(service, conn, held, other):
    """Only an automation still active is banned: a removed one has no secret left."""
    iid, token = posted(conn, held)
    conn.execute("UPDATE automations SET removed_at = ?, automation_url = NULL, signing_secret = NULL, "
                 "secret_hash = NULL WHERE registration_id = ?", (NOW, ME))
    assert service.get_prompt(token, other) == ("unknown", None)
    assert row(conn, iid)["status"] == "failed" and banned(conn) == set()


@pytest.mark.parametrize("membership,save", [("operator", "and ask the operator to save it before you ask again."),
                                             ("shared", "and save it on the account page before you ask again.")])
def test_the_ended_ask_says_who_saves_the_new_secret(conn, fake_discord, waker, held, db_path, other, membership,
                                                     save):
    """With MEMBERSHIP=operator, members can't save on the account page."""
    service = ReplyService(conn, fake_discord, make_config(database_path=db_path, membership=membership), waker,
                           held, clock=lambda: NOW)
    iid, token = posted(conn, held)
    service.get_prompt(token, other)
    assert row(conn, iid)["final_message"].endswith(save)


def test_the_foreign_log_line_names_both_memberships_and_nothing_secret(service, conn, held, other, caplog):
    iid, token = posted(conn, held, prompt="private prompt")
    with caplog.at_level("INFO", logger="outer_steppes.reply"):
        service.get_prompt(token, other)
    lines = [r.getMessage() for r in caplog.records]
    assert lines == [f"get_prompt: foreign (interaction {iid}, token owner {ME}, caller {other})"]
    assert caplog.records[0].levelname == "WARNING"
    assert token not in lines[0] and "private" not in lines[0]


def test_the_foreign_texts_equal_the_design_text():
    design = (Path(__file__).parent.parent / "DESIGN.md").read_text(encoding="utf-8")
    for message in (foreign.MSG_FOREIGN_ENDED, foreign.MSG_BLOCKED, foreign.MSG_BLOCKED_STATUS,
                    foreign.MSG_BLOCKED_NOTE):
        assert f'"{foreign.text(message, "personal")}"' in design
    for message in (foreign.MSG_SECRET_BANNED, foreign.MSG_CONNECTOR_USED, foreign.MSG_URL_IN_USE):
        assert f'"{message}"' in design
    assert '"and ask the operator to save it" in place of "and save it on the account page"' in design
