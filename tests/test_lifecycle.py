"""PR 8d-1: idle expiry, and what renews a member's idle clock (DESIGN.md, Member lifecycle)."""

from __future__ import annotations

import asyncio
from dataclasses import replace

import pytest

from outer_steppes import metrics, registry, sweeper
from outer_steppes.interactions import InteractionHandler
from outer_steppes.sweeper import apply_retention

from .conftest import CALLER, NOW, add_member, command, connect_member, make_config, register, snowflake

DAY = 86_400
IDLE_DAYS = 180
LONG_AGO = 1


def set_used(conn, user_id, at):
    conn.execute("UPDATE registrations SET last_used_at = ? WHERE discord_user_id = ?", (at, user_id))


def used(conn, user_id=CALLER):
    return conn.execute("SELECT last_used_at FROM registrations WHERE discord_user_id = ?", (user_id,)).fetchone()[0]


def active(conn, user_id):
    return registry.active_registration(conn, user_id) is not None


@pytest.fixture(autouse=True)
def fresh_metrics():
    return metrics.reset()


# ------------------------------------------------------------------ the sweep

def test_idle_expiry_ends_the_membership_with_the_full_effects_of_leaving(conn):
    idle, edge, fresh = register(conn, "1"), register(conn, "2"), register(conn, "3")
    set_used(conn, "1", NOW - IDLE_DAYS * DAY - 1)
    set_used(conn, "2", NOW - IDLE_DAYS * DAY)  # exactly at the limit: not idle yet
    set_used(conn, "3", NOW)
    conn.execute("INSERT INTO web_sessions (session_hash, discord_user_id, username, expires_at) "
                 "VALUES ('s1', '1', 'n', ?), ('s3', '3', 'n', ?)", (NOW + 600, NOW + 600))
    iid = snowflake()
    conn.execute(
        "INSERT INTO interactions (interaction_id, status, ack_kind, registration_id, automation_id, "
        "interaction_token, created_at, updated_at) VALUES (?, 'posted', 'defer', ?, ?, 'tok', ?, ?)",
        (iid, idle, idle, NOW, NOW))

    removed = apply_retention(conn, NOW, IDLE_DAYS)

    assert removed["idle_members"] == 1
    assert [active(conn, u) for u in ("1", "2", "3")] == [False, True, True]
    assert conn.execute("SELECT revoked_at FROM registrations WHERE id = ?", (idle,)).fetchone()[0] == NOW
    gone = conn.execute("SELECT automation_url, signing_secret, removed_at FROM automations WHERE registration_id = ?",
                        (idle,)).fetchone()
    assert (gone["automation_url"], gone["signing_secret"], gone["removed_at"]) == (None, None, NOW)
    assert [r[0] for r in conn.execute("SELECT session_hash FROM web_sessions")] == ["s3"]
    pending = conn.execute("SELECT status, final_message FROM interactions WHERE interaction_id = ?", (iid,)).fetchone()
    assert (pending["status"], pending["final_message"]) == ("failed", "This reply was withdrawn.")
    assert registry.active_automation(conn, edge) is not None and registry.active_automation(conn, fresh) is not None


@pytest.mark.parametrize("days", [None, 0])
def test_idle_expiry_is_off_by_default_and_with_zero(conn, days):
    register(conn, "1")
    set_used(conn, "1", LONG_AGO)
    removed = apply_retention(conn, NOW) if days is None else apply_retention(conn, NOW, days)
    assert removed["idle_members"] == 0 and active(conn, "1")


def test_a_member_with_no_last_used_never_expires(conn):
    """Creation sets it, so this can't happen. If a bug ever left it NULL, that must not expire everyone."""
    register(conn, "1")
    conn.execute("UPDATE registrations SET last_used_at = NULL")
    assert apply_retention(conn, NOW, IDLE_DAYS)["idle_members"] == 0 and active(conn, "1")


def test_each_idle_member_is_counted_after_the_commit(conn):
    for user in ("1", "2", "3"):
        register(conn, user)
        set_used(conn, user, LONG_AGO)
    set_used(conn, "3", NOW)
    apply_retention(conn, NOW, IDLE_DAYS)
    assert metrics.METRICS.membership_changes.value("self", "idle_left") == 2
    apply_retention(conn, NOW, IDLE_DAYS)  # nothing left to expire: nothing more counted
    assert metrics.METRICS.membership_changes.value("self", "idle_left") == 2


def test_an_expired_member_joins_again_like_a_new_one(conn):
    register(conn, "1")
    set_used(conn, "1", LONG_AGO)
    apply_retention(conn, NOW, IDLE_DAYS)
    member_id, created = registry.get_or_create_member_in_tx(conn, "1", NOW)
    new = registry.active_registration(conn, "1")  # the old, revoked row is still there for 30 days
    assert created and new["id"] == member_id and new["last_used_at"] == NOW


# ------------------------------------------------------------------ what renews it

def test_creating_a_member_sets_the_clock(conn):
    registry.get_or_create_member_in_tx(conn, "9", NOW)
    assert used(conn, "9") == NOW


def test_touching_a_revoked_member_changes_nothing(conn):
    member = register(conn, "1")
    registry.revoke_in_tx(conn, member, NOW)
    set_used(conn, "1", LONG_AGO)
    registry.touch_in_tx(conn, member, NOW)
    assert used(conn, "1") == LONG_AGO


@pytest.fixture
def handler(db_path, conn):
    return InteractionHandler(make_config(database_path=db_path, membership="shared"), conn,
                              posts_in_flight=lambda: 0, clock=lambda: NOW)


async def test_an_accepted_ask_renews_it(handler, conn):
    register(conn)
    connect_member(conn)
    set_used(conn, CALLER, LONG_AGO)
    assert (await handler.handle(command())).spawn is not None
    assert used(conn) == NOW


async def test_a_refused_ask_does_not(handler, conn):
    add_member(conn)  # a member with no automation: refused at ACK
    set_used(conn, CALLER, LONG_AGO)
    assert (await handler.handle(command())).spawn is None
    assert used(conn) == LONG_AGO


@pytest.mark.parametrize("private", [None, True])
async def test_settings_renews_it_whether_it_reads_or_saves(handler, conn, private):
    register(conn)
    set_used(conn, CALLER, LONG_AGO)
    await handler.handle(command("settings", private=private))
    assert used(conn) == NOW


async def test_a_non_member_using_settings_changes_nothing(handler, conn):
    register(conn)
    set_used(conn, CALLER, LONG_AGO)
    await handler.handle(command("settings", caller="999"))
    assert used(conn) == LONG_AGO


@pytest.mark.parametrize("name,sub", [("status", None), ("webhook", "status"), ("webhook", "set")])
async def test_looking_around_does_not(handler, conn, name, sub):
    register(conn)
    set_used(conn, CALLER, LONG_AGO)
    await handler.handle(command(name, sub=sub))
    assert used(conn) == LONG_AGO


class _Finalizer:
    async def run_once(self):
        return 0


async def sweeper_pass(conn, monkeypatch, days):
    """Exactly one pass of the real run_sweeper loop (its sleep is replaced to stop after the first pass)."""
    passes = []

    async def sleep(_seconds):
        if passes:
            raise asyncio.CancelledError
        passes.append(1)

    monkeypatch.setattr(sweeper.asyncio, "sleep", sleep)
    with pytest.raises(asyncio.CancelledError):
        await sweeper.run_sweeper(conn, _Finalizer(), lambda: [], 60, lambda: NOW, member_idle_days=days)


async def test_the_sweeper_loop_applies_idle_expiry(conn, monkeypatch):
    """One real pass of run_sweeper with the setting on (and, below, off)."""
    register(conn, "1")
    set_used(conn, "1", LONG_AGO)
    await sweeper_pass(conn, monkeypatch, 0)
    assert active(conn, "1")
    await sweeper_pass(conn, monkeypatch, IDLE_DAYS)
    assert not active(conn, "1")


def test_main_passes_the_configured_idle_days_to_the_sweeper():
    """The only link between MEMBER_IDLE_DAYS and the sweep. Checked on the syntax tree, so reformatting
    the call doesn't matter; dropping the argument would silently turn expiry off (the default is 0)."""
    import ast
    import inspect
    from outer_steppes import main

    calls = [node for node in ast.walk(ast.parse(inspect.getsource(main)))
             if isinstance(node, ast.Call) and getattr(node.func, "id", None) == "run_sweeper"]
    assert len(calls) == 1
    keywords = {kw.arg: ast.unparse(kw.value) for kw in calls[0].keywords}
    assert keywords.get("member_idle_days") == "config.member_idle_days"


def test_a_default_config_expires_after_180_days():
    assert make_config().member_idle_days == IDLE_DAYS
    assert replace(make_config(), member_idle_days=0).member_idle_days == 0
