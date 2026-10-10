"""A banned secret and the connector's member's warning, on every surface that shows them: the /ask refusal, the
worker's re-check, /status and /webhook status (DESIGN.md, Connector status > Another member's connector; ADR-0018).

The foreign ending itself is in test_reply.py, the save refusals in test_loopback_api.py and test_account.py.
"""

from __future__ import annotations

import base64

import pytest

from outer_steppes import foreign
from outer_steppes.interactions import InteractionHandler

from .conftest import NOW, SECRET, add_member, command, make_config, register
from .test_connector_status import add_token, text
from .test_worker import FakePoster, make_pool, queue, row, run_to_completion

BLOCKED = foreign.text(foreign.MSG_BLOCKED, make_config().membership)


def ban(conn, secret=SECRET):
    conn.execute("INSERT INTO banned_secrets (secret_hash, banned_at) VALUES (?, ?)", (foreign.secret_hash(secret), NOW))


def warn(conn, registration_id, at=NOW):
    conn.execute("UPDATE registrations SET foreign_use_at = ? WHERE id = ?", (at, registration_id))


@pytest.fixture
def config(db_path):
    return make_config(database_path=db_path)


@pytest.fixture
def handler(db_path, conn):
    return InteractionHandler(make_config(database_path=db_path), conn, posts_in_flight=lambda: 0,
                              clock=lambda: NOW)


# ------------------------------------------------------------------ the /ask refusal

async def test_ask_is_refused_while_the_automations_secret_is_banned(handler, conn):
    member = register(conn)
    add_token(conn, member)
    ban(conn)
    result = await handler.handle(command())
    assert text(result) == BLOCKED and result.body["data"]["flags"] == 64  # ephemeral, nothing accepted
    assert conn.execute("SELECT count(*) FROM rate_events").fetchone()[0] == 0


async def test_the_connection_text_comes_before_the_ban(handler, conn):
    register(conn)
    ban(conn)
    assert "isn't connected" in text(await handler.handle(command()))


async def test_another_secret_on_the_same_member_lifts_it(handler, conn):
    """Only saving a secret that isn't banned lifts it (a rotated one, or a new automation's)."""
    member = register(conn)
    add_token(conn, member)
    ban(conn, "whsec_" + base64.b64encode(b"fedcba9876543210").decode())
    assert (await handler.handle(command())).body["type"] == 5  # accepted: deferred


# ------------------------------------------------------------------ the worker's re-check

async def test_the_worker_fails_a_queued_ask_of_a_banned_automation_without_posting(conn, config):
    """Asks already queued aren't sent to the account whose run caused the ban (DESIGN.md, Re-checks)."""
    iid = queue(conn)
    ban(conn)
    poster = FakePoster()
    pool, _ = make_pool(conn, config, poster)
    await run_to_completion(pool, iid)
    assert poster.calls == []
    r = row(conn, iid)
    assert (r["status"], r["final_message"]) == ("failed", foreign.text(foreign.MSG_BLOCKED, config.membership))


# ------------------------------------------------------------------ /status and /webhook status

async def test_status_and_webhook_status_say_the_secret_is_blocked(handler, conn):
    member = register(conn)
    add_token(conn, member)
    ban(conn)
    membership = make_config().membership
    assert text(await handler.handle(command("status"))) == foreign.text(foreign.MSG_BLOCKED_STATUS, membership)
    assert text(await handler.handle(command("webhook", sub="status"))).endswith(
        " Your answers are private by default. " + foreign.text(foreign.MSG_BLOCKED_NOTE, membership))


async def test_with_no_live_token_the_connection_warning_is_shown_instead(handler, conn):
    register(conn)
    ban(conn)
    assert "blocked" not in text(await handler.handle(command("status")))
    assert "blocked" not in text(await handler.handle(command("webhook", sub="status")))


@pytest.mark.parametrize("with_automation", [True, False])
async def test_the_connectors_member_is_warned_on_every_status_surface(handler, conn, with_automation):
    """The warning isn't silenced by having no automation or a live token (DESIGN.md, Connector status)."""
    member = register(conn) if with_automation else add_member(conn)
    add_token(conn, member)
    warn(conn, member)
    status = text(await handler.handle(command("status")))
    webhook_status = text(await handler.handle(command("webhook", sub="status")))
    assert status.endswith(" " + foreign.MSG_CONNECTOR_USED) and webhook_status.endswith(" " + foreign.MSG_CONNECTOR_USED)
    if not with_automation:
        assert status == "You're a member, but you haven't set an automation yet. Run /webhook set. " \
                         + foreign.MSG_CONNECTOR_USED


def test_the_warning_lasts_a_week_or_until_a_new_connection(conn):
    member = add_member(conn)
    warn(conn, member, at=NOW)
    assert foreign.warned(conn, member, NOW)
    assert foreign.warned(conn, member, NOW + foreign.WARNING_SECONDS - 1)
    assert not foreign.warned(conn, member, NOW + foreign.WARNING_SECONDS)
    add_token(conn, member)  # connected_at == NOW: not after the foreign call
    assert foreign.warned(conn, member, NOW + 1)
    conn.execute("UPDATE oauth_tokens SET connected_at = ? WHERE registration_id = ?", (NOW + 1, member))
    assert not foreign.warned(conn, member, NOW + 2)


def test_a_member_never_warned_is_not_warned(conn):
    assert not foreign.warned(conn, add_member(conn), NOW)
