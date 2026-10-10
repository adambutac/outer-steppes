"""The admin listener's /v1 API."""

from __future__ import annotations

import base64
import hashlib
import json

import pytest

from outer_steppes import foreign, metrics, registry
from outer_steppes.db import transaction
from outer_steppes.sweeper import apply_retention
from outer_steppes.loopback_api import create_admin_app, presented_token

from .conftest import (
    ADMIN_TOKEN, AUTOMATION_URL, CALLER, NOW, SECRET, add_member, make_config, public_resolver, register,
    snowflake,
)

OTHER_SECRET = "whsec_" + base64.b64encode(b"fedcba9876543210").decode()  # made up

AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


class Waker:
    def __init__(self):
        self.count = 0

    def __call__(self):
        self.count += 1


@pytest.fixture(autouse=True)
def fresh_metrics():
    metrics.reset()


@pytest.fixture
def waker():
    return Waker()


@pytest.fixture
async def admin(aiohttp_client, db_path, conn, waker):
    config = make_config(database_path=db_path)
    app = create_admin_app(config, conn, waker, clock=lambda: NOW, resolver=public_resolver,
                           bind_hosts={"127.0.0.1"})
    return await aiohttp_client(app)


def registration_body(**overrides):
    body = {"discord_user_id": CALLER, "automation_url": AUTOMATION_URL, "signing_secret": SECRET, "label": "me"}
    body.update(overrides)
    return {k: v for k, v in body.items() if v is not None}


# ------------------------------------------------------------------ bearer

@pytest.mark.parametrize("headers,query,expected", [
    (["Bearer abc"], "", "abc"),
    (["bearer abc"], "", None),          # scheme is case-sensitive
    (["Bearer  abc"], "", None),         # whitespace inside the token
    (["Bearer "], "", None),
    (["Basic abc"], "", None),
    (["Bearer abc", "Bearer abc"], "", None),  # a second header
    ([], "", None),
    (["Bearer abc"], "token=abc", None),  # any query string
])
def test_presented_token(headers, query, expected):
    assert presented_token(headers, query) == expected


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Bearer wrong-token-wrong-token-wrong-token"},
    {"Authorization": f"bearer {ADMIN_TOKEN}"}, {"Authorization": f"Token {ADMIN_TOKEN}"},
])
async def test_bad_bearer_is_401_with_empty_body(admin, headers):
    resp = await admin.get("/v1/status", headers=headers)
    assert resp.status == 401
    assert await resp.read() == b""


async def test_query_string_is_401_even_with_good_bearer(admin):
    assert (await admin.get("/v1/status?x=1", headers=AUTH)).status == 401


async def test_unknown_v1_paths_do_not_reveal_routes_without_bearer(admin):
    assert (await admin.get("/v1/nope")).status == 401
    assert (await admin.get("/v1/nope", headers=AUTH)).status == 404


# ------------------------------------------------------------------ registrations

async def test_register_returns_ids_only(admin, conn):
    resp = await admin.post("/v1/registrations", json=registration_body(), headers=AUTH)
    assert resp.status == 200
    body = await resp.json()
    assert set(body) == {"id", "discord_user_id", "label", "created", "replaced", "withdrawn"}
    assert body["discord_user_id"] == CALLER and body["replaced"] is False
    assert body["created"] is True
    assert metrics.METRICS.membership_changes.value("operator", "joined") == 1
    assert metrics.METRICS.membership_changes.value("operator", "webhook_set") == 1
    text = await resp.text()
    assert AUTOMATION_URL not in text and SECRET not in text
    row = conn.execute("SELECT automation_url, signing_secret FROM automations WHERE removed_at IS NULL").fetchone()
    assert tuple(row) == (AUTOMATION_URL, SECRET)


def ban(conn, secret=SECRET):
    conn.execute("INSERT INTO banned_secrets (secret_hash, banned_at) VALUES (?, ?)", (foreign.secret_hash(secret), NOW))


def member_ids(conn):
    return {r[0] for r in conn.execute("SELECT discord_user_id FROM registrations WHERE revoked_at IS NULL")}


async def test_a_banned_secret_is_refused_before_dns_and_creates_no_member(db_path, conn, aiohttp_client, waker):
    """DESIGN.md, Another member's connector: a read-only lookup before any DNS."""
    looked_up = []

    async def resolver(host):
        looked_up.append(host)
        return await public_resolver(host)

    admin = await aiohttp_client(create_admin_app(make_config(database_path=db_path), conn, waker,
                                                  clock=lambda: NOW, resolver=resolver, bind_hosts={"127.0.0.1"}))
    ban(conn)
    resp = await admin.post("/v1/registrations", json=registration_body(), headers=AUTH)
    assert (resp.status, await resp.json()) == (400, {"error": "secret_banned"})
    assert looked_up == [] and member_ids(conn) == set()
    assert metrics.METRICS.membership_changes.value("operator", "secret_banned") == 1


async def test_a_ban_outlives_removal_leaving_and_retention(admin, conn):
    """DESIGN.md: the ban outlives the automation, its removal and the membership, and is never cleaned up, so
    leaving and joining again doesn't lift it."""
    assert (await admin.post("/v1/registrations", json=registration_body(), headers=AUTH)).status == 200
    ban(conn)
    assert (await admin.delete(f"/v1/registrations/{CALLER}/automation", headers=AUTH)).status == 200
    assert (await admin.delete(f"/v1/registrations/{CALLER}", headers=AUTH)).status == 200
    apply_retention(conn, NOW + 400 * 86_400)  # past every retention period, revoked memberships included
    resp = await admin.post("/v1/registrations", json=registration_body(), headers=AUTH)
    assert (resp.status, await resp.json()) == (400, {"error": "secret_banned"})


async def test_a_re_encoded_copy_of_a_banned_key_is_refused(admin, conn):
    """The ban is on the decoded key, which grok.com would still accept in another base64 spelling."""
    ban(conn)
    re_encoded = SECRET.replace("Zg==", "Zh==")  # the same 16 bytes: the last character's spare bits differ
    assert re_encoded != SECRET and foreign.secret_hash(re_encoded) == foreign.secret_hash(SECRET)
    resp = await admin.post("/v1/registrations", json=registration_body(signing_secret=re_encoded), headers=AUTH)
    assert (resp.status, await resp.json()) == (400, {"error": "secret_banned"})


async def test_a_ban_that_lands_during_dns_is_refused_in_the_save_transaction(db_path, conn, aiohttp_client, waker):
    """Checked again inside the save transaction, which rolls back whole: no member is created."""
    async def resolver(host):
        ban(conn)
        return await public_resolver(host)

    admin = await aiohttp_client(create_admin_app(make_config(database_path=db_path), conn, waker,
                                                  clock=lambda: NOW, resolver=resolver, bind_hosts={"127.0.0.1"}))
    resp = await admin.post("/v1/registrations", json=registration_body(), headers=AUTH)
    assert (resp.status, await resp.json()) == (400, {"error": "secret_banned"})
    assert member_ids(conn) == set()


async def test_a_trigger_url_is_never_stored_twice(admin, conn, caplog):
    """DESIGN.md, Members and automations: another member's URL is refused, and the log names both memberships."""
    holder = register(conn, user_id=snowflake(), url=AUTOMATION_URL, secret=OTHER_SECRET)
    with caplog.at_level("WARNING", logger="outer_steppes.loopback_api"):
        resp = await admin.post("/v1/registrations", json=registration_body(), headers=AUTH)
    assert (resp.status, await resp.json()) == (400, {"error": "url_in_use"})
    assert member_ids(conn) == {conn.execute("SELECT discord_user_id FROM registrations WHERE id = ?",
                                             (holder,)).fetchone()[0]}  # rolled back: no member created
    assert [r.getMessage() for r in caplog.records] == [
        f"registration rejected: trigger URL in use (holder {holder}, saver new)"]
    assert AUTOMATION_URL not in caplog.text
    assert metrics.METRICS.membership_changes.value("operator", "url_in_use") == 1

    # An existing member is named by its membership id, and a banned holder still holds the URL.
    saver = add_member(conn, CALLER)
    ban(conn, OTHER_SECRET)
    caplog.clear()
    with caplog.at_level("WARNING", logger="outer_steppes.loopback_api"):
        resp = await admin.post("/v1/registrations", json=registration_body(), headers=AUTH)
    assert (resp.status, await resp.json()) == (400, {"error": "url_in_use"})
    assert f"(holder {holder}, saver {saver})" in caplog.text


async def test_a_member_can_save_their_own_url_again_and_a_removed_url_is_free(admin, conn):
    assert (await admin.post("/v1/registrations", json=registration_body(), headers=AUTH)).status == 200
    rotated = registration_body(signing_secret=OTHER_SECRET)  # a rotated secret keeps the URL
    assert (await admin.post("/v1/registrations", json=rotated, headers=AUTH)).status == 200
    assert (await admin.delete(f"/v1/registrations/{CALLER}/automation", headers=AUTH)).status == 200
    other = registration_body(discord_user_id=snowflake())
    assert (await admin.post("/v1/registrations", json=other, headers=AUTH)).status == 200


@pytest.mark.parametrize("overrides,error", [
    ({"signing_secret": "not-a-secret"}, "secret_rejected"),
    ({"automation_url": "http://grok.com/webhook/automation/00000000-0000-4000-8000-000000000001"},
     "url_rejected"),
    ({"automation_url": "https://127.0.0.1/webhook/automation/00000000-0000-4000-8000-000000000001"},
     "url_rejected"),
    ({"automation_url": "https://evil.example/webhook/automation/00000000-0000-4000-8000-000000000001"},
     "url_rejected"),
    ({"discord_user_id": "abc"}, "bad_discord_user_id"),
    ({"discord_user_id": None}, "bad_discord_user_id"),
    ({"label": "x" * 101}, "bad_label"),
])
async def test_register_validation_errors_never_echo_input(admin, conn, overrides, error):
    resp = await admin.post("/v1/registrations", json=registration_body(**overrides), headers=AUTH)
    assert resp.status == 400
    assert await resp.json() == {"error": error}
    assert conn.execute("SELECT count(*) FROM registrations").fetchone()[0] == 0


async def test_register_rejects_private_dns(aiohttp_client, db_path, conn, waker):
    async def private(host):
        return ["10.0.0.5"]
    app = create_admin_app(make_config(database_path=db_path), conn, waker, clock=lambda: NOW, resolver=private)
    client = await aiohttp_client(app)
    resp = await client.post("/v1/registrations", json=registration_body(), headers=AUTH)
    assert (resp.status, await resp.json()) == (400, {"error": "url_rejected"})


async def test_register_stores_the_canonical_url(admin, conn):
    upper = AUTOMATION_URL.replace("00000000-0000-4000-8000-000000000001", "ABCDEF00-0000-4000-8000-00000000000A")
    resp = await admin.post("/v1/registrations", json=registration_body(automation_url=upper), headers=AUTH)
    assert resp.status == 200
    stored = conn.execute("SELECT automation_url FROM automations").fetchone()[0]
    assert stored == "https://grok.com/webhook/automation/abcdef00-0000-4000-8000-00000000000a"


async def test_register_malformed_json(admin):
    resp = await admin.post("/v1/registrations", data=b"not json", headers=AUTH)
    assert (resp.status, await resp.json()) == (400, {"error": "bad_request"})


async def test_revoke(admin, conn, waker):
    registration_id = register(conn)
    iid = snowflake()
    conn.execute("INSERT INTO interactions (interaction_id, status, ack_kind, registration_id, interaction_token, "
                 "created_at, updated_at) VALUES (?, 'posted', 'defer', ?, 'tok', ?, ?)",
                 (iid, registration_id, NOW, NOW))
    resp = await admin.delete(f"/v1/registrations/{CALLER}", headers=AUTH)
    assert (resp.status, await resp.json()) == (200, {"revoked": True, "withdrawn": 1})
    assert waker.count == 1
    assert (await admin.delete(f"/v1/registrations/{CALLER}", headers=AUTH)).status == 404


# ------------------------------------------------------------------ pause

async def test_pause_blocks_registration_posts_but_not_deletes(admin, conn, waker):
    register(conn)
    iid = snowflake()
    conn.execute("INSERT INTO interactions (interaction_id, status, ack_kind, interaction_token, created_at, "
                 "updated_at) VALUES (?, 'queued', 'defer', 'tok', ?, ?)", (iid, NOW, NOW))

    resp = await admin.post("/v1/pause", headers=AUTH)
    assert (resp.status, await resp.json()) == (200, {"paused": True, "withdrawn": 1})
    assert waker.count == 1

    resp = await admin.post("/v1/registrations", json=registration_body(discord_user_id="42"), headers=AUTH)
    assert resp.status == 403 and await resp.read() == b""
    # A bad bearer is still 401, not 403.
    assert (await admin.post("/v1/registrations", json=registration_body())).status == 401
    # Revocation always works.
    assert (await admin.delete(f"/v1/registrations/{CALLER}", headers=AUTH)).status == 200
    assert (await admin.post("/v1/pause", headers=AUTH)).status == 200

    status = await (await admin.get("/v1/status", headers=AUTH)).json()
    assert status == {"paused": True, "pending_final_messages": 1}

    assert (await admin.delete("/v1/pause", headers=AUTH)).status == 200
    resp = await admin.post("/v1/registrations", json=registration_body(discord_user_id="42"), headers=AUTH)
    assert resp.status == 200


# ------------------------------------------------------------------ MCP connector OAuth clients

async def test_issue_list_and_delete_mcp_clients(admin, conn):
    register(conn)
    resp = await admin.post(f"/v1/registrations/{CALLER}/mcp-clients", headers=AUTH)
    form = await resp.json()
    assert resp.status == 200
    assert form["client_id"].startswith("osc_") and form["client_secret"].startswith("oss_")
    assert form["mcp_server_url"] == "https://bridge.example/mcp"
    assert form["authorization_endpoint"] == "https://bridge.example/oauth/authorize"
    assert form["token_endpoint"] == "https://bridge.example/oauth/token"
    assert (form["scopes"], form["token_auth_method"]) == ("mcp", "client_secret_post")

    listed = await (await admin.get(f"/v1/registrations/{CALLER}/mcp-clients", headers=AUTH)).json()
    assert [c["client_id"] for c in listed] == [form["client_id"]]
    assert "client_secret" not in json.dumps(listed) and form["client_secret"] not in json.dumps(listed)

    assert (await admin.delete(f"/v1/mcp-clients/{form['client_id']}", headers=AUTH)).status == 200
    assert (await admin.delete(f"/v1/mcp-clients/{form['client_id']}", headers=AUTH)).status == 404


async def test_mcp_client_requires_registration_and_is_blocked_while_paused(admin, conn):
    assert (await admin.post("/v1/registrations/999/mcp-clients", headers=AUTH)).status == 404
    register(conn)
    await admin.post("/v1/pause", headers=AUTH)
    resp = await admin.post(f"/v1/registrations/{CALLER}/mcp-clients", headers=AUTH)
    assert resp.status == 403 and await resp.read() == b""
    assert (await admin.post(f"/v1/registrations/{CALLER}/mcp-clients")).status == 401  # bearer first


# ------------------------------------------------------------------ members and automations (PR 8b)

def posted_for(conn, registration_id):
    iid = snowflake()
    conn.execute("INSERT INTO interactions (interaction_id, status, ack_kind, registration_id, automation_id, "
                 "interaction_token, created_at, updated_at) VALUES (?, 'posted', 'defer', ?, ?, 'tok', ?, ?)",
                 (iid, registration_id, registration_id, NOW, NOW))
    return iid


async def test_replacing_a_webhook_withdraws_and_is_counted(admin, conn, waker):
    registration_id = register(conn)
    iid = posted_for(conn, registration_id)
    resp = await admin.post("/v1/registrations", json=registration_body(), headers=AUTH)
    body = await resp.json()
    assert (resp.status, body["id"], body["replaced"], body["withdrawn"]) == (200, registration_id, True, 1)
    assert conn.execute("SELECT final_message FROM interactions WHERE interaction_id = ?",
                        (iid,)).fetchone()[0] == "This reply was withdrawn."
    assert waker.count == 1
    assert metrics.METRICS.membership_changes.value("operator", "webhook_replaced") == 1
    assert metrics.METRICS.membership_changes.value("operator", "joined") == 0  # already a member


async def test_rejected_webhooks_are_counted(admin):
    await admin.post("/v1/registrations", json=registration_body(signing_secret="nope"), headers=AUTH)
    await admin.post("/v1/registrations", json=registration_body(automation_url="https://evil.example/x"),
                     headers=AUTH)
    assert metrics.METRICS.membership_changes.value("operator", "secret_rejected") == 1
    assert metrics.METRICS.membership_changes.value("operator", "url_rejected") == 1


async def test_remove_automation_keeps_the_member_and_connector(admin, conn, waker):
    registration_id = register(conn)
    conn.execute("INSERT INTO oauth_tokens (token_hash, kind, client_id, registration_id, connection_id, "
                 "connected_at, expires_at, created_at) VALUES ('t', 'refresh', 'outer-steppes', ?, 'c', ?, ?, ?)",
                 (registration_id, NOW, NOW + 60, NOW))
    iid = posted_for(conn, registration_id)
    with transaction(conn):
        registry.set_paused_in_tx(conn, True, NOW)  # works while paused, like every DELETE
    resp = await admin.delete(f"/v1/registrations/{CALLER}/automation", headers=AUTH)
    assert (resp.status, await resp.json()) == (200, {"removed": True, "withdrawn": 0})  # pause withdrew it first
    assert conn.execute("SELECT status FROM interactions WHERE interaction_id = ?", (iid,)).fetchone()[0] == "failed"
    assert registry.active_registration(conn, CALLER)["id"] == registration_id
    assert registry.active_automation(conn, registration_id) is None
    assert conn.execute("SELECT count(*) FROM oauth_tokens").fetchone()[0] == 1
    assert metrics.METRICS.membership_changes.value("operator", "webhook_removed") == 1


async def test_remove_automation_withdraws_and_is_idempotent(admin, conn, waker):
    registration_id = register(conn)
    posted_for(conn, registration_id)
    resp = await admin.delete(f"/v1/registrations/{CALLER}/automation", headers=AUTH)
    assert (resp.status, await resp.json()) == (200, {"removed": True, "withdrawn": 1})
    assert waker.count == 1
    resp = await admin.delete(f"/v1/registrations/{CALLER}/automation", headers=AUTH)
    assert (resp.status, await resp.json()) == (200, {"removed": False, "withdrawn": 0})


async def test_remove_automation_for_a_non_member_is_404(admin):
    resp = await admin.delete(f"/v1/registrations/{CALLER}/automation", headers=AUTH)
    assert (resp.status, await resp.json()) == (404, {"error": "not_found"})


# ------------------------------------------------------------------ members

async def test_list_members_shows_state_but_never_secrets(admin, conn):
    with_webhook = register(conn, user_id=snowflake())
    conn.execute("UPDATE registrations SET label = 'alice' WHERE id = ?", (with_webhook,))
    for token, connection, used in (("t1", "c1", NOW + 5), ("t2", "c1", NOW + 9), ("t3", "c2", None)):
        conn.execute("INSERT INTO oauth_tokens (token_hash, kind, client_id, registration_id, connection_id, "
                     "connected_at, expires_at, created_at, last_used_at) VALUES (?, 'refresh', 'outer-steppes', "
                     "?, ?, ?, ?, ?, ?)", (token, with_webhook, connection, NOW, NOW + 60, NOW, used))
    no_webhook = add_member(conn, snowflake())
    removed = register(conn, user_id=snowflake())
    conn.execute("UPDATE automations SET removed_at = ? WHERE registration_id = ?", (NOW, removed))
    left = register(conn, user_id=snowflake())
    conn.execute("UPDATE registrations SET revoked_at = ? WHERE id = ?", (NOW, left))
    banned = register(conn, user_id=snowflake(), secret=OTHER_SECRET)
    conn.execute("INSERT INTO banned_secrets (secret_hash, banned_at) VALUES (?, ?)",
                 (foreign.secret_hash(OTHER_SECRET), NOW))

    resp = await admin.get("/v1/registrations", headers=AUTH)
    assert resp.status == 200
    text = await resp.text()
    assert "grok.com/webhook" not in text and SECRET not in text and "whsec_" not in text and "t1" not in text
    members = {m["discord_user_id"]: m for m in json.loads(text)}
    ids = {row[0]: row[1] for row in conn.execute("SELECT id, discord_user_id FROM registrations")}
    # the one who left isn't listed
    assert set(members) == {ids[with_webhook], ids[no_webhook], ids[removed], ids[banned]}
    assert members[ids[with_webhook]] == {
        "id": with_webhook, "discord_user_id": ids[with_webhook], "label": "alice", "created_at": NOW,
        "automation": "grok_webhook", "secret_banned": False, "connections": 2, "last_used_at": NOW + 9}
    # The membership id log lines carry, and whether the secret is banned (DESIGN.md, Another member's connector).
    assert members[ids[banned]]["id"] == banned and members[ids[banned]]["secret_banned"] is True
    assert members[ids[no_webhook]]["secret_banned"] is False
    assert members[ids[no_webhook]]["automation"] is None and members[ids[no_webhook]]["connections"] == 0
    assert members[ids[removed]]["automation"] is None and members[ids[removed]]["last_used_at"] is None


async def test_list_members_needs_the_bearer(admin):
    resp = await admin.get("/v1/registrations")
    assert resp.status == 401


async def test_list_members_is_oldest_first_then_by_id(admin, conn):
    late, early_b, early_a = snowflake(), "200000000000000002", "200000000000000001"
    for user_id, created in ((late, NOW + 10), (early_b, NOW), (early_a, NOW)):
        registration_id = add_member(conn, user_id)
        conn.execute("UPDATE registrations SET created_at = ? WHERE id = ?", (created, registration_id))
    resp = await admin.get("/v1/registrations", headers=AUTH)
    assert [m["discord_user_id"] for m in await resp.json()] == [early_a, early_b, late]


# ------------------------------------------------------------------ Grok Bot (ADR-0020)

BOT_KEY = "crsr_" + hashlib.sha256(b"made-up grok bot key").hexdigest()  # made up
BOT_URL = "https://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0"


async def test_a_grok_bot_webhook_is_saved_as_its_own_kind(admin, conn):
    resp = await admin.post("/v1/registrations", json=registration_body(automation_url=BOT_URL,
                                                                        signing_secret=BOT_KEY), headers=AUTH)
    assert resp.status == 200
    row = conn.execute("SELECT kind, automation_url, secret_hash FROM automations WHERE removed_at IS NULL").fetchone()
    assert tuple(row) == ("grok_bot_webhook", BOT_URL, foreign.secret_hash(BOT_KEY))
    listed = (await (await admin.get("/v1/registrations", headers=AUTH)).json())[0]
    assert listed["automation"] == "grok_bot_webhook" and BOT_KEY not in json.dumps(listed)


@pytest.mark.parametrize("url,secret", [(BOT_URL, SECRET), (AUTOMATION_URL, BOT_KEY)])
async def test_a_secret_of_the_other_kind_is_refused(admin, conn, url, secret):
    resp = await admin.post("/v1/registrations", json=registration_body(automation_url=url, signing_secret=secret),
                            headers=AUTH)
    assert (resp.status, await resp.json()) == (400, {"error": "secret_rejected"})
    assert member_ids(conn) == set()


async def test_a_banned_grok_bot_key_is_refused(admin, conn):
    ban(conn, BOT_KEY)
    resp = await admin.post("/v1/registrations", json=registration_body(automation_url=BOT_URL,
                                                                        signing_secret=BOT_KEY), headers=AUTH)
    assert (resp.status, await resp.json()) == (400, {"error": "secret_banned"})
