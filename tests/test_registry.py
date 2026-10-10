"""Members, automations, the route predicate, revoke, and pause."""

from __future__ import annotations

import hashlib
import sqlite3

import pytest

from outer_steppes import foreign, registry
from outer_steppes.db import transaction

from .conftest import AUTOMATION_URL, CALLER, NOW, SECRET, add_member, register, snowflake


@pytest.mark.parametrize("secret", [SECRET, "whsec_" + "A" * 24, "whsec_" + "A" * 43 + "="])
def test_valid_secrets(secret):
    registry.validate_secret(secret, "grok_webhook")


@pytest.mark.parametrize("secret", [
    None, "", "whsec_", "MDEyMzQ1Njc4OWFiY2RlZg==", "WHSEC_MDEyMzQ1Njc4OWFiY2RlZg==",
    "whsec_not base64!", "whsec_QUJD",  # 3 bytes: too short
])
def test_invalid_secrets(secret):
    with pytest.raises(registry.SecretRejected):
        registry.validate_secret(secret, "grok_webhook")


def test_upsert_creates_then_updates(conn):
    with transaction(conn):
        first = registry.upsert_registration_in_tx(conn, CALLER, AUTOMATION_URL, SECRET, "me", NOW)
    assert first["discord_user_id"] == CALLER
    with transaction(conn):
        second = registry.upsert_registration_in_tx(conn, CALLER, AUTOMATION_URL, SECRET, "renamed", NOW + 1)
    assert second["id"] == first["id"]
    assert conn.execute("SELECT count(*) FROM registrations").fetchone()[0] == 1
    assert "automation_url" not in first and "signing_secret" not in first


def test_revoke_wipes_secrets_tokens_and_withdraws(conn):
    registration_id = register(conn)
    conn.execute("INSERT INTO oauth_clients (client_id, registration_id, secret_hash, created_at) "
                 "VALUES ('osc_1', ?, 'h', ?)", (registration_id, NOW))
    conn.execute("INSERT INTO oauth_tokens (token_hash, kind, client_id, registration_id, connection_id, "
                 "connected_at, expires_at, created_at) VALUES ('t', 'access', 'osc_1', ?, 'con_1', ?, ?, ?)",
                 (registration_id, NOW, NOW + 60, NOW))
    # A public-client token has no oauth_clients row; revoking must still remove it.
    conn.execute("INSERT INTO oauth_tokens (token_hash, kind, client_id, registration_id, connection_id, "
                 "connected_at, expires_at, created_at) VALUES ('t2', 'access', 'outer-steppes', ?, 'con_2', ?, ?, ?)",
                 (registration_id, NOW, NOW + 60, NOW))
    other = register(conn, "42")
    rows = {}
    for label, reg, status in (("q", registration_id, "queued"), ("p", registration_id, "posted"),
                               ("a", registration_id, "answering"), ("x", other, "queued")):
        rows[label] = snowflake()
        conn.execute("INSERT INTO interactions (interaction_id, status, ack_kind, registration_id, "
                     "interaction_token, created_at, updated_at) VALUES (?, ?, 'defer', ?, 'tok', ?, ?)",
                     (rows[label], status, reg, NOW, NOW))
    with transaction(conn):
        withdrawn = registry.revoke_in_tx(conn, registration_id, NOW)
    assert withdrawn == 2
    status = {k: conn.execute("SELECT status, final_message FROM interactions WHERE interaction_id = ?",
                              (v,)).fetchone() for k, v in rows.items()}
    assert tuple(status["q"]) == ("failed", "This reply was withdrawn.")
    assert tuple(status["p"]) == ("failed", "This reply was withdrawn.")
    assert status["a"]["status"] == "answering"   # a delivery in progress finishes
    assert status["x"]["status"] == "queued"      # another owner is untouched
    assert conn.execute("SELECT revoked_at FROM registrations WHERE id = ?", (registration_id,)).fetchone()[0] == NOW
    automation = conn.execute("SELECT * FROM automations WHERE registration_id = ?", (registration_id,)).fetchone()
    assert (automation["removed_at"], automation["automation_url"], automation["signing_secret"]) == (NOW, None, None)
    assert conn.execute("SELECT count(*) FROM oauth_clients").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM oauth_tokens").fetchone()[0] == 0  # issued and public alike


def test_reregistering_after_revoke_creates_a_new_row(conn):
    register(conn)
    with transaction(conn):
        registry.revoke_in_tx(conn, "reg-" + CALLER, NOW)
        result = registry.upsert_registration_in_tx(conn, CALLER, AUTOMATION_URL, SECRET, None, NOW)
    assert result["id"] != "reg-" + CALLER
    assert conn.execute("SELECT count(*) FROM registrations").fetchone()[0] == 2


def test_pause_withdraws_everything_queued_or_posted(conn):
    ids = [snowflake() for _ in range(3)]
    for iid, status in zip(ids, ("queued", "posted", "answering")):
        conn.execute("INSERT INTO interactions (interaction_id, status, ack_kind, interaction_token, "
                     "created_at, updated_at) VALUES (?, ?, 'defer', 'tok', ?, ?)", (iid, status, NOW, NOW))
    with transaction(conn):
        assert registry.set_paused_in_tx(conn, True, NOW) == 2
    finals = [conn.execute("SELECT final_message FROM interactions WHERE interaction_id = ?", (i,)).fetchone()[0]
              for i in ids]
    assert finals == ["Grok is paused. This request was withdrawn."] * 2 + [None]
    assert registry.pending_final_messages(conn) == 2
    with transaction(conn):
        assert registry.set_paused_in_tx(conn, False, NOW) == 0
    assert conn.execute("SELECT value FROM settings WHERE key = 'paused'").fetchone()[0] == "0"


# ------------------------------------------------------------------ route predicate

def test_own_route(conn):
    register(conn)
    route = registry.resolve_route(conn, CALLER)
    assert route == registry.Route("reg-" + CALLER, CALLER, "reg-" + CALLER)


def test_not_a_member_wording_follows_membership(conn):
    assert registry.resolve_route(conn, CALLER, membership="shared") == registry.Refusal(
        "You're not a member yet. Add the Outer Steppes connector in grok.com and sign in with Discord.")
    assert registry.resolve_route(conn, CALLER, membership="operator") == registry.Refusal(
        "You're not a member yet. Ask the operator to add you.")


def test_member_without_an_automation_is_refused(conn):
    add_member(conn)
    assert registry.resolve_route(conn, CALLER) == registry.Refusal(
        "You haven't set an automation yet. Run /webhook set.")


def test_recheck_refuses_a_row_recorded_for_another_registration(conn):
    """Defense in depth: even with the caller's own automation id, a row whose registration isn't the
    caller's current one never passes (DESIGN.md, Route predicate, Re-checks)."""
    register(conn)
    register(conn, "42")
    iid = snowflake()
    conn.execute("INSERT INTO interactions (interaction_id, status, ack_kind, caller_id, registration_id, "
                 "automation_id, interaction_token, created_at, updated_at) "
                 "VALUES (?, 'queued', 'defer', ?, 'reg-42', ?, 'tok', ?, ?)", (iid, CALLER, "reg-" + CALLER, NOW, NOW))
    row = conn.execute("SELECT * FROM interactions WHERE interaction_id = ?", (iid,)).fetchone()
    assert registry.recheck(conn, row, frozenset()) == "Not authorized."


# ------------------------------------------------------------------ members and automations (PR 8b)

def interaction(conn, status, registration_id, automation_id):
    iid = snowflake()
    conn.execute("INSERT INTO interactions (interaction_id, status, ack_kind, registration_id, automation_id, "
                 "interaction_token, created_at, updated_at) VALUES (?, ?, 'defer', ?, ?, 'tok', ?, ?)",
                 (iid, status, registration_id, automation_id, NOW, NOW))
    return iid


def final(conn, iid):
    return tuple(conn.execute("SELECT status, final_message FROM interactions WHERE interaction_id = ?",
                              (iid,)).fetchone())


def test_get_or_create_member_makes_one_member(conn):
    with transaction(conn):
        first, created = registry.get_or_create_member_in_tx(conn, CALLER, NOW)
        again, created_again = registry.get_or_create_member_in_tx(conn, CALLER, NOW)
    assert (created, created_again, again) == (True, False, first)
    assert registry.active_automation(conn, first) is None  # a member needs no webhook
    assert registry.member_count(conn) == 1


def test_replacing_removes_the_old_automation_first_and_withdraws_its_work(conn):
    registration_id = register(conn)
    conn.execute("INSERT INTO member_settings (discord_user_id, answers_private, updated_at) VALUES (?, 0, ?)",
                 (CALLER, NOW))
    conn.execute("INSERT INTO oauth_tokens (token_hash, kind, client_id, registration_id, connection_id, "
                 "connected_at, expires_at, created_at) VALUES ('t', 'refresh', 'outer-steppes', ?, 'c', ?, ?, ?)",
                 (registration_id, NOW, NOW + 60, NOW))
    queued = interaction(conn, "queued", registration_id, registration_id)
    posted = interaction(conn, "posted", registration_id, registration_id)
    answering = interaction(conn, "answering", registration_id, registration_id)
    with transaction(conn):  # identical values: still a replacement
        replaced, withdrawn = registry.set_automation_in_tx(conn, registration_id, AUTOMATION_URL, SECRET, NOW + 1)
    assert (replaced, withdrawn) == (True, 2)
    assert final(conn, queued) == final(conn, posted) == ("failed", "This reply was withdrawn.")
    assert final(conn, answering)[0] == "answering"  # a delivery in progress finishes
    old = conn.execute("SELECT * FROM automations WHERE id = ?", (registration_id,)).fetchone()
    assert (old["removed_at"], old["automation_url"], old["signing_secret"]) == (NOW + 1, None, None)
    new = registry.active_automation(conn, registration_id)
    assert new["id"] != registration_id and new["automation_url"] == AUTOMATION_URL
    # The member, their settings, and their connector stay.
    member = registry.active_registration(conn, CALLER)
    assert member["id"] == registration_id
    assert conn.execute("SELECT count(*) FROM member_settings").fetchone()[0] == 1
    assert conn.execute("SELECT count(*) FROM oauth_tokens").fetchone()[0] == 1


def test_remove_automation_keeps_the_member_and_is_idempotent(conn):
    registration_id = register(conn)
    posted = interaction(conn, "posted", registration_id, registration_id)
    with transaction(conn):
        assert registry.remove_automation_in_tx(conn, registration_id, NOW) == (True, 1)
    with transaction(conn):
        assert registry.remove_automation_in_tx(conn, registration_id, NOW) == (False, 0)
    assert final(conn, posted) == ("failed", "This reply was withdrawn.")
    assert registry.active_automation(conn, registration_id) is None
    assert registry.active_registration(conn, CALLER)["id"] == registration_id


def test_only_one_active_automation_per_member(conn):
    registration_id = register(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO automations (id, registration_id, kind, automation_url, signing_secret, created_at) "
                     "VALUES ('second', ?, 'grok_webhook', ?, ?, ?)", (registration_id, AUTOMATION_URL, SECRET, NOW))


def test_operator_upsert_gives_an_existing_member_an_automation(conn):
    registration_id = add_member(conn)
    with transaction(conn):
        result = registry.upsert_registration_in_tx(conn, CALLER, AUTOMATION_URL, SECRET, None, NOW)
    assert (result["id"], result["created"], result["replaced"], result["withdrawn"]) == (
        registration_id, False, False, 0)
    assert registry.active_automation(conn, registration_id)["automation_url"] == AUTOMATION_URL


# ------------------------------------------------------------------ the secret's format follows the kind (ADR-0020)

BOT_KEY = "crsr_" + hashlib.sha256(b"made-up grok bot key").hexdigest()  # made up


def test_the_secret_format_follows_the_urls_kind():
    registry.validate_secret(BOT_KEY, "grok_bot_webhook")
    registry.validate_secret(SECRET, "grok_webhook")
    for secret, kind in ((SECRET, "grok_bot_webhook"), (BOT_KEY, "grok_webhook"),
                         (BOT_KEY.upper().replace("CRSR_", "crsr_"), "grok_bot_webhook"), (BOT_KEY[:-1], None)):
        with pytest.raises(registry.SecretRejected):
            registry.validate_secret(secret, kind)
    registry.validate_secret(BOT_KEY, None)  # a URL that matches no provider is refused next, by the URL policy
    registry.validate_secret(SECRET, None)


def test_the_kind_comes_from_the_url():
    assert registry.kind_for_url("https://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0") == "grok_bot_webhook"
    assert registry.kind_for_url("https://grok.com/webhook/automation/00000000-0000-4000-8000-000000000001") \
        == "grok_webhook"
    assert registry.kind_for_url("https://example.com/x") is None


def test_a_grok_bot_key_is_hashed_as_it_stands():
    assert foreign.secret_hash(BOT_KEY) == hashlib.sha256(BOT_KEY.encode()).hexdigest()
