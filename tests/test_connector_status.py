"""The connector's status, and the /ask refusal at ACK (DESIGN.md, Member lifecycle > Connector status).

Answers arrive only through the connector (ADR-0017), so both apply to every member with an automation."""

from __future__ import annotations

import pytest

from outer_steppes import registry
from outer_steppes.interactions import InteractionHandler

from .conftest import CALLER, NOW, add_member, command, make_config, register

READY = "You're a member and your automation is set"
DISCONNECTED = (READY + ", but grok.com's connector isn't connected. Add or reconnect **Outer Steppes** in "
                "grok.com's connector settings, then ask again.")
REFUSAL = ("grok.com's connector isn't connected. Add or reconnect **Outer Steppes** in grok.com's connector "
           "settings, then ask again.")


def add_token(conn, registration_id, *, kind="refresh", expires=NOW + 3600, name="t"):
    conn.execute("INSERT INTO oauth_tokens (token_hash, kind, client_id, registration_id, connection_id, "
                 "connected_at, expires_at, created_at) VALUES (?, ?, 'outer-steppes', ?, 'c', ?, ?, ?)",
                 (name, kind, registration_id, NOW, expires, NOW))


@pytest.fixture
def handler(db_path, conn):
    return InteractionHandler(make_config(database_path=db_path), conn, posts_in_flight=lambda: 0,
                              clock=lambda: NOW)


def text(result) -> str:
    return result.body["data"]["content"]


# ------------------------------------------------------------------ what "live" means

@pytest.mark.parametrize("kind,expires,expected", [
    ("refresh", NOW + 1, True),        # any unexpired refresh token
    ("refresh", NOW, False),           # expires_at == now is expired
    ("refresh", NOW - 1, False),
    ("access", NOW + 3600, False),     # an access token alone is not a connection
])
def test_a_live_connection_is_an_unexpired_refresh_token(conn, kind, expires, expected):
    member = register(conn)
    add_token(conn, member, kind=kind, expires=expires)
    assert registry.connector_live(conn, member, NOW) is expected


def test_another_members_token_is_not_mine(conn):
    mine, other = register(conn, "1"), register(conn, "2")
    add_token(conn, other)
    assert not registry.connector_live(conn, mine, NOW) and registry.connector_live(conn, other, NOW)


# ------------------------------------------------------------------ /status and /webhook status

async def test_status_never_claims_a_live_connector(handler, conn):
    """The bridge can't see a connector removed inside grok.com, so a live token earns silence, not "connected"."""
    member = register(conn)
    add_token(conn, member)
    assert text(await handler.handle(command("status"))) == READY + "."
    webhook_status = text(await handler.handle(command("webhook", sub="status")))
    assert webhook_status.endswith(" Your answers are private by default.") and "connector" not in webhook_status


async def test_status_warns_a_member_with_an_automation_and_no_connector(handler, conn):
    """Including one who never connected: there's no other way for their answers to arrive."""
    member = register(conn)
    assert text(await handler.handle(command("status"))) == DISCONNECTED
    assert text(await handler.handle(command("webhook", sub="status"))).endswith(
        " Your answers are private by default. grok.com's connector isn't connected. Add or reconnect **Outer "
        "Steppes** in grok.com's connector settings.")
    add_token(conn, member, expires=NOW)  # an expired refresh token is no connection
    assert text(await handler.handle(command("status"))) == DISCONNECTED


async def test_webhook_status_says_nothing_about_the_connector_without_an_automation(handler, conn):
    add_member(conn)
    assert "connector" not in text(await handler.handle(command("webhook", sub="status")))


async def test_the_warning_keeps_the_paused_prefix(handler, conn):
    register(conn)
    conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    assert text(await handler.handle(command("status"))) == "Grok is paused. " + DISCONNECTED


async def test_reading_the_status_never_renews_the_idle_clock(handler, conn):
    register(conn)
    conn.execute("UPDATE registrations SET last_used_at = 1")
    await handler.handle(command("status"))
    await handler.handle(command("webhook", sub="status"))
    assert conn.execute("SELECT last_used_at FROM registrations").fetchone()[0] == 1


# ------------------------------------------------------------------ the /ask refusal at ACK

async def test_ask_is_refused_for_a_member_with_no_connector(handler, conn):
    register(conn)  # never connected
    conn.execute("UPDATE registrations SET last_used_at = 1")
    result = await handler.handle(command())
    assert text(result) == REFUSAL and result.body["type"] == 4  # an answer, not a deferral
    assert result.spawn is None  # no worker, so no signed POST
    assert conn.execute("SELECT count(*) FROM rate_events").fetchone()[0] == 0  # not counted against the member
    row = conn.execute("SELECT status, ack_content FROM interactions").fetchone()
    assert (row["status"], row["ack_content"]) == ("done", REFUSAL)
    assert conn.execute("SELECT last_used_at FROM registrations").fetchone()[0] == 1  # a refusal isn't use


async def test_ask_proceeds_with_a_live_connector(handler, conn):
    member = register(conn)
    add_token(conn, member)
    assert (await handler.handle(command())).spawn is not None


async def test_an_expired_connection_is_refused_like_none(handler, conn):
    member = register(conn)
    add_token(conn, member, expires=NOW - 5)
    assert text(await handler.handle(command())) == REFUSAL


async def test_the_automation_refusal_comes_first(handler, conn):
    add_member(conn)  # a member with no automation and no connector
    assert text(await handler.handle(command())) == "You haven't set an automation yet. Run /webhook set."


async def test_a_refused_ask_is_replayed_unchanged(handler, conn):
    register(conn)
    interaction = command()
    first = await handler.handle(interaction)
    assert (await handler.handle(interaction)).body == first.body


def test_the_connector_is_only_checked_for_an_active_member(conn):
    member = register(conn)
    registry.revoke_in_tx(conn, member, NOW)
    assert registry.connector_refusal(conn, member, NOW) is None
