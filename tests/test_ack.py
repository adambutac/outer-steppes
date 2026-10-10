"""The ACK transaction: dedup, identity, allowlists, pause, admission, accept."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from outer_steppes import registry
from outer_steppes.interactions import InteractionHandler, ephemeral_message, prompt_mac

from .conftest import (AUTOMATION_URL, CALLER, CHANNEL, GUILD, NOW, SECRET, add_member, command,
                       connect_member, make_config, register, snowflake)

# One prompt_mac key for every handler a test makes, so hand-built rows can match; production makes one per startup.
KEY = b"k" * 32


@pytest.fixture
def config(db_path):
    return make_config(database_path=db_path)


@pytest.fixture(autouse=True)
def caller_is_registered(conn):
    """Most tests exercise admission for a registered member with the connector connected."""
    registration_id = register(conn, CALLER)
    connect_member(conn, CALLER)
    return registration_id


def handler_for(config, conn, clock=lambda: NOW, posts_in_flight=0) -> InteractionHandler:
    return InteractionHandler(config, conn, posts_in_flight=lambda: posts_in_flight, clock=clock, prompt_key=KEY)


@pytest.fixture
def handler(config, conn):
    return handler_for(config, conn)


def row(conn, interaction_id):
    return conn.execute("SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)).fetchone()


def content(result) -> str:
    return result.body["data"]["content"]


# ------------------------------------------------------------------ accept

async def test_accepted_ask_is_queued_with_all_fields(handler, conn):
    interaction = command(prompt="line one\n\n  line   two", private=True)
    result = await handler.handle(interaction)

    assert result.body == {"type": 5, "data": {"flags": 64}}
    assert result.spawn.interaction_id == interaction["id"]
    assert result.spawn.prompt == "line one\n\n  line   two"

    r = row(conn, interaction["id"])
    assert (r["status"], r["ack_kind"], r["private"]) == ("queued", "defer", 1)
    assert (r["guild_id"], r["channel_id"], r["caller_id"]) == (GUILD, CHANNEL, CALLER)
    assert r["interaction_token"] == "interaction-token-abc"
    assert r["prompt_mac"] == prompt_mac(KEY, "line one line two")  # whitespace collapsed, then keyed
    assert r["correlation_id"].startswith("corr_") and len(r["correlation_id"]) == 37
    assert (r["registration_id"], r["owner_id"]) == ("reg-" + CALLER, CALLER)
    kinds = sorted(tuple(x) for x in conn.execute("SELECT subject_kind, subject_id FROM rate_events"))
    assert kinds == [("caller", CALLER), ("owner", CALLER)]


def test_prompt_mac_collapses_whitespace_and_depends_on_the_key():
    assert prompt_mac(KEY, "a\nb\r\nc\t d") == prompt_mac(KEY, "a b c d")
    assert prompt_mac(KEY, "a b") != prompt_mac(KEY, "a c")
    assert prompt_mac(KEY, "a b") != prompt_mac(b"j" * 32, "a b")


async def test_prompt_is_never_stored(handler, conn):
    secretish = "please summarize my inbox about project zephyr " * 10
    interaction = command(prompt=secretish)
    await handler.handle(interaction)
    dump = "\n".join(str(tuple(r)) for r in conn.execute("SELECT * FROM interactions"))
    assert "zephyr" not in dump  # not even an excerpt (ADR-0017)
    assert len(row(conn, interaction["id"])["prompt_mac"]) == 64


# ------------------------------------------------------------------ dedup

async def test_duplicate_of_deferred_ask_replays_type_5_and_does_not_spawn(handler, conn):
    interaction = command(private=True)
    first = await handler.handle(interaction)
    second = await handler.handle(interaction)
    assert first.spawn is not None
    assert second.body == first.body == {"type": 5, "data": {"flags": 64}}
    assert second.spawn is None
    assert conn.execute("SELECT count(*) FROM rate_events").fetchone()[0] == 2


async def test_duplicate_of_refusal_replays_identical_json(handler):
    interaction = command("frobnicate")
    first = await handler.handle(interaction)
    second = await handler.handle(interaction)
    assert second.body == first.body
    assert second.body["data"]["allowed_mentions"] == {"parse": []}
    assert second.body["data"]["flags"] == 64


async def test_concurrent_duplicates_spawn_exactly_once(handler):
    interaction = command()
    results = await asyncio.gather(*(handler.handle(interaction) for _ in range(10)))
    assert sum(r.spawn is not None for r in results) == 1
    assert all(r.body == {"type": 5, "data": {"flags": 64}} for r in results)  # private by default


def test_every_type_4_body_suppresses_mentions():
    body = ephemeral_message("<@123> hi @everyone")
    assert body["data"]["allowed_mentions"] == {"parse": []}


# ------------------------------------------------------------------ identity

@pytest.mark.parametrize("mutate", [
    lambda i: i["member"].pop("user"),
    lambda i: i.pop("member"),
    lambda i: i.__setitem__("user", {"id": "999"}),
    lambda i: i.pop("guild_id"),
    lambda i: i["member"].__setitem__("user", "not-an-object"),
])
async def test_identity_failures(handler, conn, mutate):
    interaction = command()
    mutate(interaction)
    result = await handler.handle(interaction)
    assert content(result) == "Unsupported invocation."
    assert result.spawn is None
    assert row(conn, interaction["id"])["status"] == "done"


async def test_matching_top_level_user_is_accepted(handler):
    interaction = command()
    interaction["user"] = {"id": CALLER}
    assert (await handler.handle(interaction)).spawn is not None


@pytest.mark.parametrize("app_id", ["999999999999999999", None])
async def test_application_id_mismatch_is_refused_without_db(handler, conn, caplog, app_id):
    interaction = command()
    interaction["application_id"] = app_id
    with caplog.at_level("ERROR"):
        result = await handler.handle(interaction)
    assert content(result) == "The bridge is misconfigured. Tell the operator to check the server log."
    assert result.spawn is None
    assert conn.execute("SELECT count(*) FROM interactions").fetchone()[0] == 0
    assert "DISCORD_APPLICATION_ID does not match" in caplog.text


async def test_non_command_types_are_unsupported_without_db(handler, conn):
    result = await handler.handle({"type": 3, "id": snowflake()})
    assert content(result) == "Unsupported interaction."
    assert conn.execute("SELECT count(*) FROM interactions").fetchone()[0] == 0


# ------------------------------------------------------------------ allowlists and pause

async def test_guild_not_allowlisted(handler):
    result = await handler.handle(command(guild="999"))
    assert content(result) == "This server isn't enabled for Grok."


async def test_channel_allowlist_uses_thread_parent(config, conn):
    handler = handler_for(replace(config, channel_allowlist=frozenset({"555"})), conn)
    in_thread = command(channel={"id": "777", "type": 11, "parent_id": "555"})
    assert (await handler.handle(in_thread)).spawn is not None
    elsewhere = command(channel={"id": "666", "type": 0})
    assert content(await handler.handle(elsewhere)) == "Not allowed here."
    # A non-thread channel's own parent_id (its category) is not used.
    category_child = command(channel={"id": "888", "type": 0, "parent_id": "555"})
    assert content(await handler.handle(category_child)) == "Not allowed here."


async def test_user_allowlist(config, conn):
    handler = handler_for(replace(config, user_allowlist=frozenset({"1"})), conn)
    assert content(await handler.handle(command())) == "Not allowed here."


async def test_pause_refuses_ask_but_not_status(handler, conn):
    conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    assert content(await handler.handle(command())) == "Grok is paused."
    assert content(await handler.handle(command("status"))) == "Grok is paused. You're a member and your automation is set."


async def test_leave_runs_before_allowlists_and_pause(handler, conn):
    conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    assert content(await handler.handle(command("leave", guild="999", confirm=True))).startswith("You've left")


async def test_unknown_command(handler):
    assert content(await handler.handle(command("frobnicate"))) == "Unsupported command."


@pytest.mark.parametrize("prompt", [None, "", "   "])
async def test_ask_without_prompt(handler, prompt):
    assert content(await handler.handle(command(prompt=prompt))) == "Unsupported invocation."


# ------------------------------------------------------------------ admission

def insert_row(conn, *, status: str, caller=CALLER, channel=CHANNEL, posted_at=None, created_at=NOW,
               prompt="earlier question"):
    conn.execute(
        "INSERT INTO interactions (interaction_id, status, ack_kind, guild_id, channel_id, caller_id, "
        "prompt_mac, posted_at, created_at, updated_at) VALUES (?, ?, 'defer', ?, ?, ?, ?, ?, ?, ?)",
        (snowflake(), status, GUILD, channel, caller, prompt_mac(KEY, prompt), posted_at, created_at, created_at),
    )


TOO_MANY_HERE = "Too many of your messages are still running here. Wait for one to finish."


@pytest.mark.parametrize("status", ["queued", "answering"])
async def test_per_channel_limit_counts_queued_and_answering(conn, config, status):
    handler = handler_for(replace(config, per_channel_max_asks=2), conn)
    insert_row(conn, status=status, prompt="one")
    assert (await handler.handle(command(prompt="two"))).spawn is not None
    assert content(await handler.handle(command(prompt="three"))) == TOO_MANY_HERE


async def test_per_channel_limit_defaults_to_three_parallel_asks(handler):
    for prompt in ("one", "two", "three"):
        assert (await handler.handle(command(prompt=prompt))).spawn is not None
    assert content(await handler.handle(command(prompt="four"))) == TOO_MANY_HERE


async def test_per_channel_limit_of_one_is_single_flight(conn, config):
    handler = handler_for(replace(config, per_channel_max_asks=1), conn)
    insert_row(conn, status="queued")
    assert content(await handler.handle(command(prompt="something else"))) == TOO_MANY_HERE


async def test_per_channel_posted_window(conn, config):
    insert_row(conn, status="posted", posted_at=NOW)
    config = replace(config, per_channel_max_asks=1)
    at_30 = handler_for(config, conn, clock=lambda: NOW + 30)
    assert content(await at_30.handle(command())) == TOO_MANY_HERE
    at_91 = handler_for(config, conn, clock=lambda: NOW + 91)
    assert (await at_91.handle(command())).spawn is not None


async def test_per_channel_limit_is_per_channel_and_caller(conn, config):
    handler = handler_for(replace(config, per_channel_max_asks=1), conn)
    insert_row(conn, status="queued")
    assert (await handler.handle(command(channel={"id": "999", "type": 0}))).spawn is not None
    insert_row(conn, status="queued", caller="42")
    register(conn, "43")
    connect_member(conn, "43")
    assert (await handler.handle(command(caller="43", channel={"id": "998", "type": 0}))).spawn is not None


@pytest.mark.parametrize("status", ["answered", "failed", "expired", "done"])
async def test_per_channel_limit_ignores_finished_rows(conn, config, status):
    handler = handler_for(replace(config, per_channel_max_asks=1), conn)
    insert_row(conn, status=status, prompt="hello")
    assert (await handler.handle(command(prompt="hello"))).spawn is not None


async def test_same_prompt_is_refused_while_the_first_is_live(handler):
    assert (await handler.handle(command(prompt="what is  the\nweather"))).spawn is not None
    # Compared after whitespace is collapsed, as the MAC is computed.
    assert content(await handler.handle(command(prompt="what is the weather"))) == \
        "Already working on your last message here."
    assert (await handler.handle(command(prompt="what is the weather today"))).spawn is not None


async def test_same_prompt_runs_again_after_the_posted_window(conn, config):
    insert_row(conn, status="posted", posted_at=NOW, prompt="hello")
    at_30 = handler_for(config, conn, clock=lambda: NOW + 30)
    assert content(await at_30.handle(command(prompt="hello"))) == "Already working on your last message here."
    at_91 = handler_for(config, conn, clock=lambda: NOW + 91)
    assert (await at_91.handle(command(prompt="hello"))).spawn is not None


async def test_same_prompt_in_another_channel_runs(handler):
    assert (await handler.handle(command(prompt="hello"))).spawn is not None
    assert (await handler.handle(command(prompt="hello", channel={"id": "999", "type": 0}))).spawn is not None


async def test_repeat_is_refused_before_the_count(conn, config):
    handler = handler_for(replace(config, per_channel_max_asks=1), conn)
    insert_row(conn, status="queued", prompt="hello")
    assert content(await handler.handle(command(prompt="hello"))) == "Already working on your last message here."


async def test_repeat_of_an_answering_ask_is_refused(handler, conn):
    insert_row(conn, status="answering", prompt="hello")
    assert content(await handler.handle(command(prompt="hello"))) == "Already working on your last message here."


async def test_prompts_that_differ_only_after_200_characters_are_not_a_repeat(handler):
    """The whole prompt is compared, through its MAC: nothing is cut to an excerpt any more."""
    shared = "x" * 200
    assert (await handler.handle(command(prompt=shared + " first"))).spawn is not None
    assert (await handler.handle(command(prompt=shared + " second"))).spawn is not None


async def test_after_a_restart_the_same_prompt_is_not_a_repeat(conn, config):
    """Accepted gap (DESIGN.md, ACK step 11): the key is made at startup, so an ask from before a restart doesn't
    match."""
    insert_row(conn, status="queued", prompt="hello")
    restarted = InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=lambda: NOW)  # a fresh key
    assert (await restarted.handle(command(prompt="hello"))).spawn is not None


async def test_caller_rate_per_minute(conn, config):
    handler = handler_for(replace(config, per_caller_rpm=2, per_channel_posted_seconds=0), conn)
    for _ in range(2):
        interaction = command(channel={"id": snowflake(), "type": 0})
        assert (await handler.handle(interaction)).spawn is not None
    assert content(await handler.handle(command(channel={"id": snowflake(), "type": 0}))) == "Slow down."


async def test_caller_rate_windows_slide(conn, config):
    cfg = replace(config, per_caller_rpm=1, per_caller_daily=2)
    for offset in (0, 61):
        h = handler_for(cfg, conn, clock=lambda o=offset: NOW + o)
        assert (await h.handle(command(channel={"id": snowflake(), "type": 0}))).spawn is not None
    later = handler_for(cfg, conn, clock=lambda: NOW + 200)
    assert content(await later.handle(command(channel={"id": snowflake(), "type": 0}))) == "Slow down."
    next_day = handler_for(cfg, conn, clock=lambda: NOW + 86_401)
    assert (await next_day.handle(command(channel={"id": snowflake(), "type": 0}))).spawn is not None


async def test_queue_cap_counts_queued_rows_and_posts_in_flight(conn, config):
    cfg = replace(config, max_concurrent_automation_posts=1, max_queued=1)
    insert_row(conn, status="queued", caller="1")
    full = handler_for(cfg, conn, posts_in_flight=1)
    assert content(await full.handle(command())) == "Busy, try again."
    room = handler_for(cfg, conn, posts_in_flight=0)
    assert (await room.handle(command())).spawn is not None


async def test_posted_rows_do_not_count_toward_queue(conn, config):
    cfg = replace(config, max_concurrent_automation_posts=1, max_queued=0)
    for i in range(5):
        insert_row(conn, status="posted", caller=str(i), posted_at=NOW - 1000)
    assert (await handler_for(cfg, conn).handle(command())).spawn is not None


async def test_busy_database_answers_busy_without_writing(config, db_path, conn):
    from outer_steppes import db as dbmod
    ack_conn = dbmod.connect(db_path, 50)
    handler = handler_for(config, ack_conn)
    conn.execute("BEGIN IMMEDIATE")  # another writer holds the lock
    try:
        result = await handler.handle(command())
    finally:
        conn.execute("ROLLBACK")
        ack_conn.close()
    assert content(result) == "Busy, try again."
    assert conn.execute("SELECT count(*) FROM interactions").fetchone()[0] == 0


# ------------------------------------------------------------------ PR 3: route and owner checks

async def test_ask_without_registration_is_not_authorized(handler, conn):
    result = await handler.handle(command(caller="999"))
    assert content(result) == "You're not a member yet. Ask the operator to add you."
    assert result.spawn is None


async def test_revoked_registration_is_not_a_route(handler, conn):
    conn.execute("UPDATE registrations SET revoked_at = ? WHERE discord_user_id = ?", (NOW, CALLER))
    assert content(await handler.handle(command())) == "You're not a member yet. Ask the operator to add you."


async def test_owner_rate_limit(conn, config):
    handler = handler_for(replace(config, per_owner_rpm=1, per_caller_rpm=5), conn)
    assert (await handler.handle(command(channel={"id": snowflake(), "type": 0}))).spawn is not None
    assert content(await handler.handle(command(channel={"id": snowflake(), "type": 0}))) == "Slow down."


async def test_outstanding_cap_counts_every_unanswered_row_for_the_registration(conn, config):
    registration_id = "reg-" + CALLER
    for status in ("queued", "posted", "answering", "posted"):
        conn.execute(
            "INSERT INTO interactions (interaction_id, status, ack_kind, guild_id, channel_id, caller_id, "
            "registration_id, posted_at, created_at, updated_at) VALUES (?, ?, 'defer', ?, ?, '1', ?, ?, ?, ?)",
            (snowflake(), status, GUILD, snowflake(), registration_id, NOW - 1000, NOW, NOW),
        )
    handler = handler_for(replace(config, per_owner_max_outstanding=5), conn)
    assert (await handler.handle(command(channel={"id": snowflake(), "type": 0}))).spawn is not None
    refused = await handler.handle(command(channel={"id": snowflake(), "type": 0}))
    assert content(refused) == "This automation has too many unanswered requests. Try again later."
    # A row becoming terminal frees a slot.
    conn.execute("UPDATE interactions SET status = 'answered' WHERE status = 'answering'")
    assert (await handler.handle(command(channel={"id": snowflake(), "type": 0}))).spawn is not None


# ------------------------------------------------------------------ /status

async def test_leave_withdraws_in_flight_and_asks_to_wake_the_finalizer(handler, conn):
    registration_id = "reg-" + CALLER
    ids = []
    for status in ("queued", "posted", "posted", "answering"):
        iid = snowflake()
        ids.append(iid)
        conn.execute(
            "INSERT INTO interactions (interaction_id, status, ack_kind, registration_id, interaction_token, "
            "created_at, updated_at) VALUES (?, ?, 'defer', ?, 'tok', ?, ?)",
            (iid, status, registration_id, NOW, NOW),
        )
    result = await handler.handle(command("leave", confirm=True))
    assert content(result).startswith("You've left Outer Steppes. Pending answers won't be posted")
    assert "connector settings" in content(result) and "automation" in content(result)
    assert result.wake_finalizer is True
    statuses = [row(conn, iid)["status"] for iid in ids]
    assert statuses == ["failed", "failed", "failed", "answering"]  # answering finishes
    assert all(row(conn, iid)["final_message"] == "This reply was withdrawn." for iid in ids[:3])
    reg = conn.execute("SELECT * FROM registrations WHERE id = ?", (registration_id,)).fetchone()
    assert reg["revoked_at"] == NOW
    automation = conn.execute("SELECT * FROM automations WHERE registration_id = ?", (reg["id"],)).fetchone()
    assert automation["automation_url"] is None and automation["signing_secret"] is None


async def test_leave_by_a_non_member_gets_the_not_a_member_text(handler):
    assert content(await handler.handle(command("leave", caller="999", confirm=True))) == \
        "You're not a member yet. Ask the operator to add you."


async def test_leave_with_nothing_in_flight_does_not_wake(handler):
    assert (await handler.handle(command("leave", confirm=True))).wake_finalizer is False


@pytest.mark.parametrize("confirm", [None, False])
async def test_bare_leave_only_explains_and_changes_nothing(handler, conn, confirm):
    result = await handler.handle(command("leave", confirm=confirm))
    assert content(result).startswith("Leaving Outer Steppes deletes your automation")
    assert "/leave confirm:True" in content(result) and result.membership_change is None
    assert registry.active_registration(conn, CALLER) is not None
    assert registry.active_automation(conn, "reg-" + CALLER) is not None


async def test_status(handler):
    assert content(await handler.handle(command("status"))) == "You're a member and your automation is set."
    assert content(await handler.handle(command("status", caller="999"))) == \
        "You're not a member yet. Ask the operator to add you."


async def test_status_and_ask_for_a_member_without_an_automation(config, conn):
    add_member(conn, "999")
    handler = handler_for(config, conn)
    assert content(await handler.handle(command("status", caller="999"))) == \
        "You're a member, but you haven't set an automation yet. Run /webhook set."
    result = await handler.handle(command(caller="999"))
    assert content(result) == "You haven't set an automation yet. Run /webhook set."
    assert result.spawn is None  # no deferral, no signed POST
    assert conn.execute("SELECT count(*) FROM rate_events").fetchone()[0] == 0


async def test_not_a_member_text_points_at_the_connector_when_shared(config, conn):
    handler = handler_for(replace(config, membership="shared"), conn)
    expected = "You're not a member yet. Add the Outer Steppes connector in grok.com and sign in with Discord."
    assert content(await handler.handle(command(caller="999"))) == expected
    assert content(await handler.handle(command("status", caller="999"))) == expected


async def test_accepted_ask_records_its_automation(handler, conn):
    result = await handler.handle(command())
    row = conn.execute("SELECT automation_id FROM interactions WHERE interaction_id = ?",
                       (result.spawn.interaction_id,)).fetchone()
    assert row["automation_id"] == registry.active_automation(conn, "reg-" + CALLER)["id"]


async def test_leave_is_counted_as_a_self_leave_after_commit(handler):
    from outer_steppes import metrics
    metrics.reset()
    await handler.handle(command("leave"))  # bare: explains only, nothing to count
    assert metrics.METRICS.membership_changes.value("self", "left") == 0
    await handler.handle(command("leave", confirm=True))
    assert metrics.METRICS.membership_changes.value("self", "left") == 1
    await handler.handle(command("leave", confirm=True))  # no longer a member: nothing to count
    assert metrics.METRICS.membership_changes.value("self", "left") == 1


# ------------------------------------------------------------------ /webhook (PR 8c-2)

PAUSE = "INSERT INTO settings (key, value) VALUES ('paused', '1')"


@pytest.fixture
def shared_handler(config, conn):
    return handler_for(replace(config, membership="shared"), conn)


async def test_webhook_set_replies_with_only_the_account_link_and_host(shared_handler):
    result = await shared_handler.handle(command("webhook", sub="set"))
    assert result.body["data"]["flags"] == 64 | 4  # ephemeral, embeds suppressed
    assert result.body["data"]["allowed_mentions"] == {"parse": []}
    text = content(result)
    # The host is named in plain text, apart from the link, so a member learns it (DESIGN.md, The account page).
    assert text == ("Set or replace your automation on the account page: https://bridge.example/account\n"
                    "Check that the address starts with bridge.example before pasting a secret. "
                    "Never paste a webhook URL or secret in Discord.")
    assert AUTOMATION_URL not in text and SECRET not in text


async def test_webhook_set_is_the_same_when_replayed(shared_handler):
    interaction = command("webhook", sub="set")
    first = await shared_handler.handle(interaction)
    # A fresh handler on the same database, as after a restart: only the stored ack_content survives.
    replayed = await handler_for(replace(shared_handler._config), shared_handler._conn).handle(interaction)
    assert replayed.body == first.body and replayed.body["data"]["flags"] == 64 | 4


async def test_webhook_set_gives_a_non_member_the_link_because_login_creates_the_member(shared_handler):
    result = await shared_handler.handle(command("webhook", sub="set", caller="999"))
    assert "https://bridge.example/account" in content(result)


@pytest.mark.parametrize("caller", [CALLER, "999"])
async def test_webhook_set_with_membership_operator_says_to_ask_the_operator(handler, caller):
    result = await handler.handle(command("webhook", sub="set", caller=caller))
    assert content(result) == "Ask the operator to set your automation."
    assert result.body["data"]["flags"] == 64


async def test_webhook_status_never_shows_the_url_or_secret(handler, conn):
    conn.execute("UPDATE automations SET created_at = 1700000000")
    text = content(await handler.handle(command("webhook", sub="status")))
    assert text == "Your automation is set (since <t:1700000000:f>). Your answers are private by default."
    assert AUTOMATION_URL not in text and SECRET not in text


async def test_webhook_status_for_a_member_without_an_automation_and_for_a_non_member(config, conn):
    add_member(conn, "999")
    handler = handler_for(config, conn)
    assert content(await handler.handle(command("webhook", sub="status", caller="999"))) == \
        "You haven't set an automation yet. Run /webhook set. Your answers are private by default."
    assert content(await handler.handle(command("webhook", sub="status", caller="888"))) == \
        "You're not a member yet. Ask the operator to add you."


async def test_webhook_status_answers_while_paused_but_set_does_not(shared_handler, conn):
    conn.execute(PAUSE)
    status = content(await shared_handler.handle(command("webhook", sub="status")))
    assert status.startswith("Grok is paused. Your automation is set")
    assert content(await shared_handler.handle(command("webhook", sub="set"))) == "Grok is paused."


async def test_webhook_remove_matches_the_loopback_delete(handler, conn):
    registration_id = "reg-" + CALLER
    iid = snowflake()
    conn.execute(
        "INSERT INTO interactions (interaction_id, status, ack_kind, registration_id, automation_id, "
        "interaction_token, created_at, updated_at) VALUES (?, 'posted', 'defer', ?, ?, 'tok', ?, ?)",
        (iid, registration_id, registry.active_automation(conn, registration_id)["id"], NOW, NOW),
    )
    result = await handler.handle(command("webhook", sub="remove"))
    assert content(result) == ("Automation removed. You're still a member, and this doesn't "
                               "disconnect grok.com's connector.")
    assert result.wake_finalizer is True and result.membership_change == "webhook_removed"
    assert registry.active_automation(conn, registration_id) is None
    assert registry.active_registration(conn, CALLER) is not None  # still a member
    assert row(conn, iid)["status"] == "failed" and row(conn, iid)["final_message"] == "This reply was withdrawn."
    gone = conn.execute("SELECT automation_url, signing_secret FROM automations").fetchone()
    assert gone["automation_url"] is None and gone["signing_secret"] is None


async def test_webhook_remove_is_idempotent_and_for_members_only(handler):
    await handler.handle(command("webhook", sub="remove"))
    again = await handler.handle(command("webhook", sub="remove"))
    assert content(again) == "You have no automation set. Nothing to remove."
    assert again.wake_finalizer is False and again.membership_change is None
    assert content(await handler.handle(command("webhook", sub="remove", caller="999"))) == \
        "You're not a member yet. Ask the operator to add you."


async def test_webhook_remove_and_leave_ignore_pause_and_the_user_allowlist(config, conn):
    conn.execute(PAUSE)
    handler = handler_for(replace(config, user_allowlist=frozenset({"1"})), conn)
    assert content(await handler.handle(command("leave"))).startswith("Leaving Outer Steppes deletes")
    assert content(await handler.handle(command("webhook", sub="remove"))).startswith("Automation removed.")
    assert content(await handler.handle(command("leave", confirm=True))).startswith("You've left")
    # The same caller, still outside the allowlist, is refused everything else.
    assert content(await handler.handle(command("status"))) == "Not allowed here."


async def test_webhook_without_a_known_subcommand_is_unsupported(handler):
    assert content(await handler.handle(command("webhook"))) == "Unsupported command."
    assert content(await handler.handle(command("webhook", sub="frobnicate"))) == "Unsupported command."


async def test_webhook_remove_is_counted_after_commit(handler):
    from outer_steppes import metrics
    metrics.reset()
    await handler.handle(command("webhook", sub="remove"))
    await handler.handle(command("webhook", sub="remove"))  # nothing left to remove
    assert metrics.METRICS.membership_changes.value("self", "webhook_removed") == 1
