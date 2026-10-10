"""User and guild installs (DESIGN.md, Install contexts)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from outer_steppes import registry
from outer_steppes.interactions import InteractionHandler, resolve_invocation

from .conftest import APP_ID, CALLER, GUILD, NOW, connect_member, make_config, register, snowflake

OTHER_GUILD = "555555555555555555"
DM_CHANNEL = "666666666666666666"


def payload(*, context, guild=None, member=True, user=None, owners=None, name="ask", prompt="hello",
            private=None, channel=None):
    options = []
    if name == "leave":
        options.append({"name": "confirm", "type": 5, "value": True})
    if name == "ask":
        options.append({"name": "prompt", "type": 3, "value": prompt})
    if private is not None:
        options.append({"name": "private", "type": 5, "value": private})
    body = {
        "id": snowflake(), "type": 2, "token": "itok", "application_id": APP_ID, "context": context,
        "channel_id": channel or DM_CHANNEL, "channel": {"id": channel or DM_CHANNEL, "type": 0 if guild else 1},
        "data": {"name": name, "type": 1, "options": options},
    }
    if guild is not None:
        body["guild_id"] = guild
    if member:
        body["member"] = {"user": {"id": CALLER}, "roles": []}
    if user is not None:
        body["user"] = {"id": user}
    if owners is not None:
        body["authorizing_integration_owners"] = owners
    return body


def guild_installed(**kw):
    return payload(context=0, guild=GUILD, owners={"0": GUILD}, **kw)


def user_installed_in(guild, **kw):
    return payload(context=0, guild=guild, owners={"1": CALLER}, **kw)


def bot_dm(**kw):
    return payload(context=1, member=False, user=CALLER, owners={"1": CALLER}, **kw)


def group_dm(**kw):
    return payload(context=2, member=False, user=CALLER, owners={"1": CALLER}, **kw)


@pytest.fixture
def config(db_path):
    return make_config(database_path=db_path)


@pytest.fixture(autouse=True)
def registered(conn):
    registration_id = register(conn, CALLER)
    connect_member(conn, CALLER)  # answers arrive only through the connector (ADR-0017)
    return registration_id


def handler_for(config, conn):
    return InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=lambda: NOW)


def row(conn, interaction_id):
    return conn.execute("SELECT * FROM interactions WHERE interaction_id = ?", (interaction_id,)).fetchone()


def content(result):
    return result.body["data"]["content"]


# ------------------------------------------------------------------ identity

@pytest.mark.parametrize("body,expected", [
    (guild_installed(), (CALLER, GUILD, 0, True)),
    (user_installed_in(OTHER_GUILD), (CALLER, OTHER_GUILD, 0, False)),
    (payload(context=0, guild=GUILD, owners={"0": GUILD, "1": CALLER}), (CALLER, GUILD, 0, True)),
    (bot_dm(), (CALLER, None, 1, False)),
    (group_dm(), (CALLER, None, 2, False)),
])
def test_resolve_invocation(body, expected):
    inv = resolve_invocation(body)
    assert (inv.caller_id, inv.guild_id, inv.context, inv.guild_installed) == expected


@pytest.mark.parametrize("body", [
    payload(context=1, member=True, user=CALLER),                  # DM payloads carry no member
    payload(context=0, guild=GUILD, member=False, user=CALLER),    # a server payload needs a member
    payload(context=0, guild=GUILD, user="999"),                   # member and user disagree
    payload(context=0, member=False, user=CALLER),                 # server context without a server
    payload(context=1, guild=GUILD),                               # DM context with a server
    payload(context=1, member=False),                              # nobody at all
    payload(context=1, member=False, user="not-a-snowflake"),
])
def test_malformed_identity_is_refused(body):
    assert resolve_invocation(body) is None


# ------------------------------------------------------------------ where /ask works

PRIVATE, PUBLIC = {"type": 5, "data": {"flags": 64}}, {"type": 5}
EVERY_CONTEXT = (bot_dm, group_dm, lambda **kw: user_installed_in(OTHER_GUILD, **kw), guild_installed)


async def test_bot_dm_works(config, conn):
    body = bot_dm()
    assert (await handler_for(config, conn).handle(body)).body == PRIVATE
    r = row(conn, body["id"])
    assert (r["guild_id"], r["guild_installed"], r["private"], r["caller_id"]) == (None, 0, 1, CALLER)


@pytest.mark.parametrize("make", EVERY_CONTEXT)
async def test_private_by_default_everywhere(config, conn, make):
    assert (await handler_for(config, conn).handle(make())).body == PRIVATE


@pytest.mark.parametrize("make", EVERY_CONTEXT)
async def test_explicit_private_false_shares_the_answer(config, conn, make):
    assert (await handler_for(config, conn).handle(make(private=False))).body == PUBLIC


# ------------------------------------------------------------------ /settings

async def test_settings_shows_the_private_default(config, conn):
    text = content(await handler_for(config, conn).handle(bot_dm(name="settings")))
    assert text.startswith("Your answers are private by default")


@pytest.mark.parametrize("make", EVERY_CONTEXT)
async def test_public_setting_applies_everywhere_and_ask_still_overrides(config, conn, make):
    handler = handler_for(config, conn)
    saved = content(await handler.handle(bot_dm(name="settings", private=False)))
    assert saved.startswith("Saved. Your answers are public by default")
    assert (await handler.handle(make())).body == PUBLIC
    # Another channel, so the same prompt isn't refused as a repeat.
    assert (await handler.handle(make(private=True, channel="777777777777777777"))).body == PRIVATE


async def test_settings_can_switch_back_to_private(config, conn):
    handler = handler_for(config, conn)
    await handler.handle(bot_dm(name="settings", private=False))
    await handler.handle(bot_dm(name="settings", private=True))
    assert (await handler.handle(guild_installed())).body == PRIVATE


async def test_settings_needs_a_registration(config, conn):
    conn.execute("DELETE FROM registrations")
    text = content(await handler_for(config, conn).handle(bot_dm(name="settings", private=False)))
    assert text == "Not authorized."
    assert conn.execute("SELECT count(*) FROM member_settings").fetchone()[0] == 0


async def test_settings_work_while_paused(config, conn):
    conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    text = content(await handler_for(config, conn).handle(bot_dm(name="settings", private=False)))
    assert text.startswith("Saved.")


async def test_unregistered_member_is_refused_everywhere(config, conn):
    conn.execute("DELETE FROM registrations")
    handler = handler_for(config, conn)
    for body in (bot_dm(), group_dm(), user_installed_in(OTHER_GUILD), guild_installed()):
        assert content(await handler.handle(body)) == "You're not a member yet. Ask the operator to add you."


# ------------------------------------------------------------------ allowlists

async def test_guild_allowlist_applies_only_where_the_app_is_installed(config, conn):
    handler = handler_for(replace(config, guild_allowlist=frozenset({GUILD})), conn)
    # A server that installed the app but isn't listed is refused.
    installed_elsewhere = payload(context=0, guild=OTHER_GUILD, owners={"0": OTHER_GUILD})
    assert content(await handler.handle(installed_elsewhere)) == "This server isn't enabled for Grok."
    # A member's own install works in that same server.
    assert (await handler.handle(user_installed_in(OTHER_GUILD))).spawn is not None


async def test_empty_guild_allowlist_allows_any_installing_server(config, conn):
    handler = handler_for(replace(config, guild_allowlist=frozenset()), conn)
    body = payload(context=0, guild=OTHER_GUILD, owners={"0": OTHER_GUILD})
    assert (await handler.handle(body)).spawn is not None


async def test_channel_allowlist_applies_only_where_the_app_is_installed(config, conn):
    handler = handler_for(replace(config, channel_allowlist=frozenset({"1"})), conn)
    assert content(await handler.handle(guild_installed(channel="2"))) == "Not allowed here."
    assert (await handler.handle(user_installed_in(OTHER_GUILD, channel="2"))).spawn is not None


async def test_user_allowlist_applies_everywhere(config, conn):
    handler = handler_for(replace(config, user_allowlist=frozenset({"1"})), conn)
    for body in (bot_dm(), group_dm(), user_installed_in(OTHER_GUILD), guild_installed()):
        assert content(await handler.handle(body)) == "Not allowed here."


# ------------------------------------------------------------------ other commands and the per-channel limit

async def test_status_and_leave_work_in_dms(config, conn):
    handler = handler_for(config, conn)
    assert content(await handler.handle(bot_dm(name="status"))) == "You're a member and your automation is set."
    assert content(await handler.handle(bot_dm(name="leave"))).startswith("You've left Outer Steppes.")


async def test_repeat_prompt_in_a_dm(config, conn):
    handler = handler_for(config, conn)
    assert (await handler.handle(bot_dm())).spawn is not None
    assert content(await handler.handle(bot_dm())) == "Already working on your last message here."


# ------------------------------------------------------------------ re-check (worker and reply)

def stored(conn, body):
    return row(conn, body["id"])


async def test_recheck_applies_guild_allowlist_only_to_guild_installs(config, conn):
    handler = handler_for(config, conn)
    installed, own_install, dm = guild_installed(), user_installed_in(OTHER_GUILD), bot_dm()
    for body in (installed, own_install):
        await handler.handle(body)
    await handler_for(config, conn).handle(dm)
    only_other = frozenset({"123"})
    assert registry.recheck(conn, stored(conn, installed), only_other) == "Not authorized."
    assert registry.recheck(conn, stored(conn, own_install), only_other) is None
    assert registry.recheck(conn, stored(conn, dm), only_other) is None
    assert registry.recheck(conn, stored(conn, installed), frozenset()) is None
