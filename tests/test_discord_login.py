"""PR 8a: Discord login for the grok.com connector (DESIGN.md, Discord login for the connector)."""

from __future__ import annotations

import asyncio
import html
import logging
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest
from aiohttp import web

from outer_steppes import metrics, oauth, registry
from outer_steppes.config import ConfigError, load_config
from outer_steppes.db import transaction
from outer_steppes.discord_login import DiscordLogin, DiscordLoginError, DiscordUser
from outer_steppes.http_app import create_public_app
from outer_steppes.interactions import InteractionHandler
from outer_steppes.loopback_api import create_admin_app
from outer_steppes.mcp_server import McpServer
from outer_steppes.oauth import LOGIN_COOKIE, OAuthServer, pkce_s256
from outer_steppes.reply import HeldPrompts, ReplyService

from .conftest import (ADMIN_TOKEN, APP_ID, CALLER, NOW, Clock, add_member, claim_bridge, make_config, page_link,
                       register)

CALLBACK = "https://grok.com/connectors-oauth-exchange-code/"
RESOURCE = "https://bridge.example/mcp"
VERIFIER = "v" * 43
PUBLIC = "outer-steppes"
CLIENT_SECRET = "discord-oauth-client-secret-0123456789"  # made up; shaped like Discord's (no dots)


class FakeIdentifier:
    """Stands in for DiscordLogin: returns a user, or raises."""

    def __init__(self, user: DiscordUser | None = DiscordUser(CALLER, "member"), fail: bool = False) -> None:
        self.user, self.fail, self.codes = user, fail, []

    async def identify(self, code: str) -> DiscordUser:
        self.codes.append(code)
        if self.fail:
            raise DiscordLoginError("down")
        return self.user


@pytest.fixture(autouse=True)
def fresh_metrics():
    return metrics.reset()


@pytest.fixture
def config(db_path):
    return make_config(database_path=db_path, discord_client_secret=CLIENT_SECRET)


@pytest.fixture
def server(conn, config, clock):
    return OAuthServer(conn, config, clock)


def authorize_params(**overrides):
    params = {"response_type": "code", "client_id": PUBLIC, "redirect_uri": CALLBACK, "state": "grok-state",
              "code_challenge": pkce_s256(VERIFIER), "code_challenge_method": "S256", "resource": RESOURCE,
              "scope": "mcp"}
    params.update(overrides)
    return {k: v for k, v in params.items() if v is not None}


def query_of(location: str) -> dict:
    return {k: v[0] for k, v in parse_qs(urlsplit(location).query).items()}


def start(server, **overrides) -> tuple[str, str]:
    """Authorize with the public client. Returns (discord state, browser nonce)."""
    result = server.authorize(authorize_params(**overrides))
    assert result.status == 200 and result.location is None and result.login_cookie  # the connect-time warning
    return query_of(result.continue_url)["state"], result.login_cookie


async def finish(server, state, cookie, identifier=None, **query):
    return await server.discord_callback({"state": state, "code": "discord-code", **query}, cookie,
                                         identifier or FakeIdentifier())


def exchange(server, code, **overrides):
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": VERIFIER,
            "resource": RESOURCE, "client_id": PUBLIC}
    form.update(overrides)
    return server.token({k: v for k, v in form.items() if v is not None}, None)


async def connect(server, conn) -> dict:
    """Registered member, full flow. Returns the token response body."""
    state, cookie = start(server)
    result = await finish(server, state, cookie)
    return exchange(server, query_of(result.location)["code"]).body


# ------------------------------------------------------------------ authorize

def test_public_client_stops_on_the_warning_with_a_fresh_discord_state(server, conn):
    result = server.authorize(authorize_params())
    assert (result.status, result.location) == (200, None)  # a page, not a redirect (DESIGN.md, Connect-time warning)
    assert result.cancel_url == CALLBACK + "?error=access_denied&state=grok-state"  # as Discord's own cancel ends
    parts = urlsplit(result.continue_url)
    assert (parts.scheme, parts.netloc, parts.path) == ("https", "discord.com", "/oauth2/authorize")
    query = query_of(result.continue_url)
    assert query == {"response_type": "code", "client_id": APP_ID, "scope": "identify",
                     "redirect_uri": "https://bridge.example/oauth/discord/callback", "state": query["state"]}
    row = conn.execute("SELECT * FROM oauth_pending").fetchone()
    assert row["state_hash"] == oauth._hash(query["state"]) and row["nonce_hash"] == oauth._hash(result.login_cookie)
    assert (row["purpose"], row["client_state"], row["expires_at"]) == ("connector", "grok-state", NOW + 600)
    assert query["state"] not in str(tuple(row)) and result.login_cookie not in str(tuple(row))  # stored hashed
    assert start(server)[0] != query["state"]  # fresh every time


@pytest.mark.parametrize("overrides,error", [
    ({"code_challenge_method": "plain"}, "invalid_request"),
    ({"resource": "https://evil.example/mcp"}, "invalid_target"),
    ({"scope": "mcp admin"}, "invalid_scope"),
    ({"response_type": "token"}, "unsupported_response_type"),
])
def test_validation_runs_before_anything_is_stored(server, conn, overrides, error):
    result = server.authorize(authorize_params(**overrides))
    assert query_of(result.location)["error"] == error and result.login_cookie is None
    assert conn.execute("SELECT count(*) FROM oauth_pending").fetchone()[0] == 0


def test_bad_callback_never_redirects(server):
    result = server.authorize(authorize_params(redirect_uri="https://evil.example/cb"))
    assert (result.status, result.location) == (400, None)


def test_public_client_does_not_exist_without_discord_login(conn, config, clock):
    server = OAuthServer(conn, replace(config, discord_client_secret=""), clock)
    result = server.authorize(authorize_params())
    assert (result.status, result.location) == (400, None)
    assert server.token({"grant_type": "authorization_code", "client_id": PUBLIC, "code": "x"}, None).status == 401


def test_pending_cap_counts_only_unexpired_rows_and_sweep_removes_them(conn, config, clock):
    server = OAuthServer(conn, replace(config, oauth_max_pending=10), clock)
    for _ in range(10):
        start(server)
    full = server.authorize(authorize_params())
    assert (full.status, full.location) == (503, None)
    assert conn.execute("SELECT count(*) FROM oauth_pending").fetchone()[0] == 10  # nothing stored past the cap
    clock.now = NOW + 601  # every pending login has expired: they no longer block anyone
    start(server)
    with transaction(conn):
        oauth.delete_expired_in_tx(conn, int(clock.now))
    assert conn.execute("SELECT count(*) FROM oauth_pending").fetchone()[0] == 1


# ------------------------------------------------------------------ callback

async def test_success_binds_the_code_to_the_signed_in_members_registration(server, conn):
    registration_id = register(conn)
    state, cookie = start(server)
    identifier = FakeIdentifier()
    result = await finish(server, state, cookie, identifier)
    assert identifier.codes == ["discord-code"]
    assert result.location.startswith(CALLBACK)
    query = query_of(result.location)
    assert query["state"] == "grok-state"
    code = conn.execute("SELECT * FROM oauth_codes").fetchone()
    assert (code["registration_id"], code["client_id"]) == (registration_id, PUBLIC)
    body = exchange(server, query["code"]).body  # auth method "none": no secret at all
    assert server.registration_for_access_token("Bearer " + body["access_token"]) == registration_id
    assert metrics.METRICS.discord_logins.value("connector", "ok") == 1


async def test_the_registration_comes_from_the_discord_identity_only(server, conn):
    register(conn)
    other = register(conn, "999999999999999999")
    state, cookie = start(server)
    result = await finish(server, state, cookie, FakeIdentifier(DiscordUser("999999999999999999", "other")))
    body = exchange(server, query_of(result.location)["code"]).body
    assert server.registration_for_access_token("Bearer " + body["access_token"]) == other


async def test_shared_public_secret_when_configured(conn, config, clock):
    register(conn)
    server = OAuthServer(conn, replace(config, oauth_public_client_secret="shared-not-secret"), clock)
    state, cookie = start(server)
    code = query_of((await finish(server, state, cookie)).location)["code"]
    assert exchange(server, code).status == 401  # now required
    state, cookie = start(server)
    code = query_of((await finish(server, state, cookie)).location)["code"]
    assert exchange(server, code, client_secret="shared-not-secret").status == 200


@pytest.mark.parametrize("case", ["unknown_state", "missing_cookie", "wrong_cookie", "expired", "reused"])
async def test_bad_state_or_cookie_gets_a_page_and_no_redirect(server, conn, clock, case):
    register(conn)
    state, cookie = start(server)
    if case == "reused":
        await finish(server, state, cookie)
    if case == "expired":
        clock.now = NOW + 601
    state = "nope" if case == "unknown_state" else state
    cookie = None if case == "missing_cookie" else "other" if case == "wrong_cookie" else cookie
    identifier = FakeIdentifier()
    result = await finish(server, state, cookie, identifier)
    assert (result.status, result.location) == (400, None)
    assert identifier.codes == [] or case == "reused"  # Discord is never called on a bad state
    # A named pending login is consumed whatever the outcome; an unknown state
    # touches nothing (the real row waits for its expiry and the sweep).
    remaining = conn.execute("SELECT count(*) FROM oauth_pending").fetchone()[0]
    assert remaining == (1 if case == "unknown_state" else 0)


async def test_member_cancels_on_discord(server, conn):
    state, cookie = start(server)
    result = await server.discord_callback({"state": state, "error": "access_denied"}, cookie, FakeIdentifier())
    assert query_of(result.location) == {"error": "access_denied", "state": "grok-state"}
    assert metrics.METRICS.discord_logins.value("connector", "denied") == 1


async def test_discord_failure_redirects_temporarily_unavailable(server, conn):
    register(conn)
    state, cookie = start(server)
    result = await finish(server, state, cookie, FakeIdentifier(fail=True))
    assert query_of(result.location) == {"error": "temporarily_unavailable", "state": "grok-state"}
    assert conn.execute("SELECT count(*) FROM oauth_codes").fetchone()[0] == 0


@pytest.mark.parametrize("setup,result_name,phrase", [
    ("unregistered", "not_registered", "only the operator adds members"),
    ("allowlist", "not_allowed", "isn't allowed here"),
    ("paused", "paused", "paused"),
])
async def test_refusals_get_a_403_page_naming_the_account(conn, config, clock, setup, result_name, phrase):
    if setup != "unregistered":
        register(conn)
    if setup == "allowlist":
        config = replace(config, user_allowlist=frozenset({"1"}))
    if setup == "paused":
        conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    server = OAuthServer(conn, config, clock)
    state, cookie = start(server)
    result = await finish(server, state, cookie, FakeIdentifier(DiscordUser(CALLER, "member")))
    assert (result.status, result.location) == (403, None)
    assert phrase in result.page and "member" in result.page
    assert "ask the operator" in result.page.lower() or setup != "unregistered"
    assert conn.execute("SELECT count(*) FROM oauth_codes").fetchone()[0] == 0
    assert metrics.METRICS.discord_logins.value("connector", result_name) == 1


# ------------------------------------------------------------------ joining a shared bridge

@pytest.fixture
def shared(conn, config, clock):
    """A shared bridge its owner has claimed: anyone else may join by signing in."""
    claim_bridge(conn)
    return OAuthServer(conn, replace(config, membership="shared"), clock)


async def test_a_non_member_joins_by_connecting_with_no_webhook(shared, conn):
    state, cookie = start(shared)
    result = await finish(shared, state, cookie)
    assert result.status == 302 and "code" in query_of(result.location)  # straight back to grok.com: no form
    member = registry.active_registration(conn, CALLER)
    assert member is not None and registry.active_automation(conn, member["id"]) is None
    token = exchange(shared, query_of(result.location)["code"])
    assert token.status == 200
    assert shared.registration_for_access_token("Bearer " + token.body["access_token"]) == member["id"]
    assert metrics.METRICS.membership_changes.value("self", "joined") == 1
    assert metrics.METRICS.discord_logins.value("connector", "ok") == 1


async def test_an_existing_member_is_not_created_again(shared, conn):
    registration_id = add_member(conn)
    state, cookie = start(shared)
    await finish(shared, state, cookie)
    assert registry.active_registration(conn, CALLER)["id"] == registration_id
    assert metrics.METRICS.membership_changes.value("self", "joined") == 0


async def test_two_concurrent_first_logins_make_one_member(shared, conn):
    first, second = start(shared), start(shared)
    results = await asyncio.gather(finish(shared, *first), finish(shared, *second))
    assert [r.status for r in results] == [302, 302]
    assert registry.member_count(conn) == 2  # the owner and the caller
    assert conn.execute("SELECT count(DISTINCT registration_id) FROM oauth_codes").fetchone()[0] == 1


async def test_concurrent_joins_never_exceed_max_members(conn, config, clock):
    claim_bridge(conn)
    server = OAuthServer(conn, replace(config, membership="shared", max_members=2), clock)
    logins = [start(server), start(server)]
    users = [FakeIdentifier(DiscordUser("5001", "a")), FakeIdentifier(DiscordUser("5002", "b"))]
    results = await asyncio.gather(*(finish(server, *login, user) for login, user in zip(logins, users)))
    assert sorted(r.status for r in results) == [302, 403]
    assert registry.member_count(conn) == 2
    assert metrics.METRICS.discord_logins.value("connector", "full") == 1


async def test_a_full_bridge_refuses_new_members_but_not_existing_ones(conn, config, clock):
    add_member(conn, "5001")
    server = OAuthServer(conn, replace(config, membership="shared", max_members=1), clock)
    state, cookie = start(server)
    result = await finish(server, state, cookie)
    assert result.status == 403 and "no room for new members" in result.page
    assert metrics.METRICS.membership_changes.value("self", "full") == 1
    assert registry.member_count(conn) == 1 and conn.execute("SELECT count(*) FROM oauth_codes").fetchone()[0] == 0
    state, cookie = start(server)
    assert (await finish(server, state, cookie, FakeIdentifier(DiscordUser("5001", "a")))).status == 302


async def test_no_member_is_created_while_paused(shared, conn):
    conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    state, cookie = start(shared)
    result = await finish(shared, state, cookie)
    assert result.status == 403 and "paused" in result.page
    assert registry.active_registration(conn, CALLER) is None
    assert metrics.METRICS.discord_logins.value("connector", "paused") == 1


async def test_the_allowlist_is_checked_before_pause(shared, conn):
    """DESIGN.md callback table: a listed-out user during a pause is counted as not_allowed."""
    conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    server = OAuthServer(conn, replace(shared._config, user_allowlist=frozenset({"1"})), lambda: NOW)
    state, cookie = start(server)
    result = await finish(server, state, cookie)
    assert result.status == 403 and "isn't allowed here" in result.page
    assert metrics.METRICS.discord_logins.value("connector", "not_allowed") == 1
    assert registry.active_registration(conn, CALLER) is None


# ------------------------------------------------------------------ tokens and connections

async def test_public_client_rejects_a_secret_it_shouldnt_have_and_basic_auth(server, conn):
    register(conn)
    state, cookie = start(server)
    code = query_of((await finish(server, state, cookie)).location)["code"]
    assert exchange(server, code, client_secret="anything").status == 401
    basic = "Basic " + __import__("base64").b64encode(f"{PUBLIC}:".encode()).decode()
    form = {"grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": VERIFIER}
    assert server.token(form, basic).status == 401


async def test_refresh_keeps_the_connection(server, conn):
    register(conn)
    first = await connect(server, conn)
    before = {r["connection_id"] for r in conn.execute("SELECT connection_id FROM oauth_tokens")}
    second = server.token({"grant_type": "refresh_token", "refresh_token": first["refresh_token"],
                           "client_id": PUBLIC}, None)
    assert second.status == 200
    after = {r["connection_id"] for r in conn.execute("SELECT connection_id FROM oauth_tokens")}
    assert before == after and len(after) == 1


async def test_connection_times_survive_refresh_and_sweep(server, conn, clock):
    """connected_at is when it started; last_used_at is the latest issue, refresh, or delivery."""
    registration_id = register(conn)
    first = await connect(server, conn)
    clock.now = NOW + 5_000  # past the 1-hour access token
    refreshed = server.token({"grant_type": "refresh_token", "refresh_token": first["refresh_token"],
                              "client_id": PUBLIC}, None).body
    with transaction(conn):
        oauth.delete_expired_in_tx(conn, int(clock.now))  # sweeps the first access token
    clock.now = NOW + 6_000
    server.touch_after_delivery(registration_id, "Bearer " + refreshed["access_token"])
    with transaction(conn):
        oauth.delete_expired_in_tx(conn, NOW + 5_000 + 3_601)  # and the refreshed access token too
    [connection] = oauth.list_connections(conn, registration_id)
    assert (connection["created_at"], connection["last_used_at"]) == (NOW, NOW + 6_000)


async def test_delivery_touches_only_the_client_that_made_the_call(server, conn, clock):
    """A member who moved from an issued client to Discord login: the old client must look unused."""
    registration_id = register(conn)
    with transaction(conn):
        issued_id, _ = oauth.issue_client_in_tx(conn, registration_id, NOW)
    body = await connect(server, conn)  # public client
    clock.now = NOW + 100
    server.touch_after_delivery(registration_id, "Bearer " + body["access_token"])
    assert oauth.list_clients(conn, registration_id)[0]["last_used_at"] is None


async def test_eviction_follows_first_connection_not_last_refresh(server, conn, clock):
    registration_id = register(conn)
    oldest = await connect(server, conn)
    for i in range(1, 5):
        clock.now = NOW + i
        await connect(server, conn)
    clock.now = NOW + 10  # the oldest connection refreshes: it is still the oldest
    server.token({"grant_type": "refresh_token", "refresh_token": oldest["refresh_token"], "client_id": PUBLIC}, None)
    clock.now = NOW + 11
    await connect(server, conn)  # the sixth
    remaining = [c["created_at"] for c in oauth.list_connections(conn, registration_id)]
    assert remaining == [NOW + 1, NOW + 2, NOW + 3, NOW + 4, NOW + 11]


async def test_the_sixth_connection_evicts_the_oldest(server, conn, clock):
    registration_id = register(conn)
    bodies = []
    for i in range(6):
        clock.now = NOW + i
        bodies.append(await connect(server, conn))
    connections = oauth.list_connections(conn, registration_id)
    assert len(connections) == 5 and all(c["client_kind"] == "public" for c in connections)
    assert server.registration_for_access_token("Bearer " + bodies[0]["access_token"]) is None
    assert server.registration_for_access_token("Bearer " + bodies[5]["access_token"]) == registration_id


async def test_revoke_removes_public_client_codes_and_tokens(server, conn):
    registration_id = register(conn)
    body = await connect(server, conn)
    state, cookie = start(server)
    await finish(server, state, cookie)  # an unused code
    with transaction(conn):
        registry.revoke_in_tx(conn, registration_id, NOW)
    assert server.registration_for_access_token("Bearer " + body["access_token"]) is None
    assert conn.execute("SELECT count(*) FROM oauth_tokens").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM oauth_codes").fetchone()[0] == 0


async def test_disconnect_one_or_all(server, conn, clock):
    registration_id = register(conn)
    first = await connect(server, conn)
    clock.now = NOW + 1
    await connect(server, conn)
    ids = [c["connection_id"] for c in oauth.list_connections(conn, registration_id)]
    with transaction(conn):
        assert oauth.delete_connections_in_tx(conn, registration_id, ids[0]) == 1
    assert server.registration_for_access_token("Bearer " + first["access_token"]) is None
    with transaction(conn):
        assert oauth.delete_connections_in_tx(conn, registration_id) == 1
    assert oauth.list_connections(conn, registration_id) == []


# ------------------------------------------------------------------ HTTP: pages, cookie, headers

@pytest.fixture
async def http(aiohttp_client, config, conn, fake_discord):
    replies = ReplyService(conn, fake_discord, config, lambda: None, HeldPrompts(), clock=lambda: NOW)
    server = OAuthServer(conn, config, clock=lambda: NOW)
    handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=lambda: NOW)
    identifier = FakeIdentifier(DiscordUser(CALLER, "<script>alert(1)</script>"))
    app = create_public_app(config, handler, lambda *_: None, clock=lambda: NOW, oauth=server, mcp=McpServer(server, replies), discord_login=identifier)
    return await aiohttp_client(app)


def assert_page_headers(resp):
    assert resp.headers["Content-Security-Policy"] == ("default-src 'none'; style-src 'unsafe-inline'; "
                                                        "form-action 'self'; frame-ancestors 'none'")
    assert resp.headers["Cache-Control"] == "no-store"
    assert resp.headers["Referrer-Policy"] == "no-referrer"


async def warning_state(resp) -> str:
    """The Discord state from the connect-time warning's Continue link."""
    assert resp.status == 200 and "Location" not in resp.headers
    return query_of(page_link(await resp.text(), "Continue"))["state"]


def cookie_attributes(header: str) -> tuple[str, dict[str, str]]:
    """A Set-Cookie header's name and its attributes (lower-case keys), minus the Expires date."""
    name = header.split("=", 1)[0]
    attributes = dict((part.strip().split("=", 1) + [""])[:2] for part in header.split(";")[1:])
    return name, {key.lower(): value for key, value in attributes.items() if key.lower() != "expires"}


async def test_http_flow_sets_the_login_cookie_and_headers(http, conn):
    register(conn)
    resp = await http.get("/oauth/authorize?" + urlencode(authorize_params()), allow_redirects=False)
    assert resp.status == 200 and "Location" not in resp.headers  # the connect-time warning page
    assert_page_headers(resp)
    assert LOGIN_COOKIE == "__Host-os_login"
    name, attributes = cookie_attributes(resp.headers["Set-Cookie"])
    # Exactly these, and no Domain: what the __Host- prefix requires (DESIGN.md, Threat model).
    assert name == LOGIN_COOKIE
    assert attributes == {"httponly": "", "max-age": "600", "path": "/", "samesite": "Lax", "secure": ""}
    nonce = resp.cookies[LOGIN_COOKIE].value
    state = await warning_state(resp)
    done = await http.get(f"/oauth/discord/callback?state={state}&code=c", allow_redirects=False,
                          headers={"Cookie": f"{LOGIN_COOKIE}={nonce}"})
    assert done.status == 302 and done.headers["Location"].startswith(CALLBACK)
    assert_page_headers(done)
    # Cleared on the same Path it was set on, or the browser would keep it.
    name, attributes = cookie_attributes(done.headers["Set-Cookie"])
    assert name == LOGIN_COOKIE and attributes["max-age"] == "0" and attributes["path"] == "/"


async def test_a_login_cookie_without_the_host_prefix_is_ignored(http, conn):
    # What a sibling subdomain could plant: the attacker's own nonce under the old, unprefixed name.
    register(conn)
    resp = await http.get("/oauth/authorize?" + urlencode(authorize_params()), allow_redirects=False)
    nonce = resp.cookies[LOGIN_COOKIE].value
    state = await warning_state(resp)
    http.session.cookie_jar.clear()  # so the planted cookie is the only one sent
    planted = await http.get(f"/oauth/discord/callback?state={state}&code=c", allow_redirects=False,
                             headers={"Cookie": f"os_login={nonce}"})
    assert planted.status == 400 and "Location" not in planted.headers


async def test_refusal_pages_escape_the_username_and_carry_headers(http, conn):
    resp = await http.get("/oauth/authorize?" + urlencode(authorize_params()), allow_redirects=False)
    nonce = resp.cookies["__Host-os_login"].value
    state = await warning_state(resp)
    page = await http.get(f"/oauth/discord/callback?state={state}&code=c", allow_redirects=False,
                          headers={"Cookie": f"__Host-os_login={nonce}"})
    assert page.status == 403 and page.content_type == "text/html"
    text = await page.text()
    assert "<script>" not in text and "&lt;script&gt;" in text
    assert_page_headers(page)


async def test_error_pages_carry_headers(http):
    bad = await http.get("/oauth/discord/callback?state=nope&code=c", allow_redirects=False)
    assert bad.status == 400 and "Location" not in bad.headers
    assert_page_headers(bad)
    bad_callback = await http.get("/oauth/authorize?client_id=outer-steppes&redirect_uri=https://evil.example/",
                                  allow_redirects=False)
    assert bad_callback.status == 400
    assert_page_headers(bad_callback)


async def test_callback_without_discord_login_is_a_bad_state_page(aiohttp_client, conn, db_path):
    config = make_config(database_path=db_path)  # login off
    replies = ReplyService(conn, None, config, lambda: None, HeldPrompts())
    server = OAuthServer(conn, config)
    handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0)
    http = await aiohttp_client(create_public_app(config, handler, lambda *_: None, oauth=server, mcp=McpServer(server, replies)))
    resp = await http.get("/oauth/discord/callback?state=x&code=y", allow_redirects=False)
    assert resp.status == 400 and "Location" not in resp.headers


async def test_login_never_logs_codes_state_cookie_or_secret(server, conn, caplog):
    register(conn)
    with caplog.at_level(logging.DEBUG):
        state, cookie = start(server)
        result = await finish(server, state, cookie)
        code = query_of(result.location)["code"]
        exchange(server, code)
    logged = caplog.text
    for secret in (state, cookie, code, "discord-code", CLIENT_SECRET):
        assert secret not in logged


# ------------------------------------------------------------------ the Discord client

@pytest.fixture
async def fake_discord_api(aiohttp_server):
    calls = []
    behavior = {"token_status": 200, "me_status": 200, "me": {"id": CALLER, "username": "member"}}

    async def token(request):
        form = await request.post()
        calls.append(("token", dict(form)))
        if behavior["token_status"] != 200:
            return web.json_response({"error": "invalid_grant"}, status=behavior["token_status"])
        return web.json_response({"access_token": "discord-access", "token_type": "Bearer", "expires_in": 604800,
                                  "refresh_token": "discord-refresh", "scope": "identify applications.commands"})

    async def me(request):
        calls.append(("me", request.headers.get("Authorization")))
        return web.json_response(behavior["me"], status=behavior["me_status"])

    async def revoke(request):
        calls.append(("revoke", None))
        return web.Response(status=200)

    app = web.Application()
    app.router.add_post("/oauth2/token", token)
    app.router.add_get("/users/@me", me)
    app.router.add_post("/oauth2/token/revoke", revoke)
    server = await aiohttp_server(app)
    return server, calls, behavior


async def login_client(fake_discord_api):
    import aiohttp
    server, _, _ = fake_discord_api
    session = aiohttp.ClientSession()
    return session, DiscordLogin(session, APP_ID, CLIENT_SECRET, "https://bridge.example/oauth/discord/callback",
                                 api_base=str(server.make_url("")))


async def test_discord_client_exchanges_reads_me_and_never_revokes(fake_discord_api, caplog):
    _, calls, _ = fake_discord_api
    session, login = await login_client(fake_discord_api)
    async with session:
        with caplog.at_level(logging.DEBUG):
            user = await login.identify("the-code")
    assert user == DiscordUser(CALLER, "member")
    kind, form = calls[0]
    assert kind == "token" and form == {"grant_type": "authorization_code", "code": "the-code",
                                        "redirect_uri": "https://bridge.example/oauth/discord/callback",
                                        "client_id": APP_ID, "client_secret": CLIENT_SECRET}
    assert calls[1] == ("me", "Bearer discord-access")
    assert [c[0] for c in calls] == ["token", "me"]  # no revoke: it would cancel the member's whole authorization
    for secret in ("the-code", "discord-access", "discord-refresh", CLIENT_SECRET):
        assert secret not in caplog.text


@pytest.mark.parametrize("change", [{"token_status": 400}, {"me_status": 401}, {"me": {"id": "not-digits"}}])
async def test_discord_client_failures_raise(fake_discord_api, change):
    _, _, behavior = fake_discord_api
    behavior.update(change)
    session, login = await login_client(fake_discord_api)
    async with session:
        with pytest.raises(DiscordLoginError):
            await login.identify("c")


# ------------------------------------------------------------------ admin API

@pytest.fixture
async def admin(aiohttp_client, config, conn):
    return await aiohttp_client(create_admin_app(config, conn, lambda: None, clock=lambda: NOW))


AUTH = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


async def test_admin_lists_and_deletes_connections(admin, conn, config):
    registration_id = register(conn)
    await connect(OAuthServer(conn, config, clock=Clock()), conn)
    listed = await (await admin.get(f"/v1/registrations/{CALLER}/mcp-connections", headers=AUTH)).json()
    assert len(listed) == 1 and listed[0]["client_kind"] == "public" and "token" not in str(listed)
    one = await admin.delete(f"/v1/registrations/{CALLER}/mcp-connections/{listed[0]['connection_id']}",
                             headers=AUTH)
    assert (await one.json()) == {"disconnected": 1}
    missing = await admin.delete(f"/v1/registrations/{CALLER}/mcp-connections/con_nope", headers=AUTH)
    assert missing.status == 404
    assert oauth.list_connections(conn, registration_id) == []


async def test_connector_settings(admin, aiohttp_client, conn, config):
    settings = await (await admin.get("/v1/connector-settings", headers=AUTH)).json()
    assert settings["client_id"] == PUBLIC and settings["client_secret"] == ""
    assert settings["token_auth_method"] == "none"
    assert settings["discord_redirect_url"] == "https://bridge.example/oauth/discord/callback"
    off = await aiohttp_client(create_admin_app(replace(config, discord_client_secret=""), conn, lambda: None))
    assert (await off.get("/v1/connector-settings", headers=AUTH)).status == 404


# ------------------------------------------------------------------ config

def env(**overrides):
    base = {"DISCORD_PUBLIC_KEY": "11" * 32, "DISCORD_APPLICATION_ID": "1", "DATABASE_PATH": "x.db",
            "PUBLIC_BASE_URL": "https://bridge.example", "BRIDGE_REGISTRATION_TOKEN": "t" * 40}
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_login_is_off_without_a_client_secret():
    config = load_config(env())
    assert not config.discord_login_enabled and config.oauth_public_client_id == PUBLIC


def test_client_secret_turns_login_on_and_the_file_form_works(tmp_path):
    assert load_config(env(DISCORD_CLIENT_SECRET=CLIENT_SECRET)).discord_login_enabled
    path = tmp_path / "secret"
    path.write_text(CLIENT_SECRET + "\n", encoding="utf-8")
    assert load_config(env(DISCORD_CLIENT_SECRET_FILE=str(path))).discord_client_secret == CLIENT_SECRET


@pytest.mark.parametrize("overrides,match", [
    ({"DISCORD_CLIENT_SECRET": "MTIz.NDU2.Nzg5"}, "looks like a bot token"),
    ({"DISCORD_CLIENT_SECRET": "a", "DISCORD_CLIENT_SECRET_FILE": "f"}, "only one"),
    ({"DISCORD_CLIENT_SECRET": "has space"}, "no whitespace"),
    ({"OAUTH_PUBLIC_CLIENT_ID": "osc_mine"}, "must not start with osc_"),
    ({"OAUTH_PUBLIC_CLIENT_ID": "bad id!"}, "OAUTH_PUBLIC_CLIENT_ID"),
    ({"OAUTH_MAX_PENDING": "5"}, "between 10 and 10000"),
    ({"MCP_MAX_CONNECTIONS_PER_REGISTRATION": "21"}, "between 1 and 20"),
])
def test_config_refusals(overrides, match):
    with pytest.raises(ConfigError, match=match):
        load_config(env(**overrides))


def test_empty_client_secret_line_means_off():
    assert not load_config(env(DISCORD_CLIENT_SECRET="")).discord_login_enabled


def test_public_client_secret_rejects_whitespace():
    with pytest.raises(ConfigError, match="OAUTH_PUBLIC_CLIENT_SECRET"):
        load_config(env(OAUTH_PUBLIC_CLIENT_SECRET="has space"))


def test_bot_token_refusal_never_echoes_the_value():
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(DISCORD_CLIENT_SECRET="MTIz.NDU2.Nzg5"))
    assert "MTIz" not in str(excinfo.value)


def test_membership_defaults_to_personal_with_discord_login_and_operator_without():
    off = load_config(env())
    on = load_config(env(DISCORD_CLIENT_SECRET=CLIENT_SECRET))
    assert (off.membership, on.membership) == ("operator", "personal")
    assert (off.members_set_automations, on.members_set_automations) == (False, True)
    assert on.max_members == 100
    for mode in ("personal", "shared", "operator", " Shared "):
        assert load_config(env(DISCORD_CLIENT_SECRET=CLIENT_SECRET, MEMBERSHIP=mode)).membership == mode.strip().lower()
    assert load_config(env(MEMBERSHIP="operator")).membership == "operator"
    assert load_config(env(MAX_MEMBERS="10000")).max_members == 10_000


@pytest.mark.parametrize("overrides,match", [
    ({"MEMBERSHIP": "personal"}, "MEMBERSHIP=personal needs Discord login"),
    ({"MEMBERSHIP": "shared"}, "MEMBERSHIP=shared needs Discord login"),
    ({"MAX_MEMBERS": "0"}, "between 1 and 10000"),
    ({"MAX_MEMBERS": "10001"}, "between 1 and 10000"),
    ({"MEMBERSHIP": "true", "DISCORD_CLIENT_SECRET": CLIENT_SECRET}, "MEMBERSHIP must be"),
    ({"MEMBERSHIP": "", "DISCORD_CLIENT_SECRET": CLIENT_SECRET}, "MEMBERSHIP must be"),
])
def test_membership_refusals(overrides, match):
    with pytest.raises(ConfigError, match=match):
        load_config(env(**overrides))


async def test_a_connector_login_renews_the_members_idle_clock(server, conn, clock):
    """PR 8d-1."""
    registration_id = register(conn)
    conn.execute("UPDATE registrations SET last_used_at = 1 WHERE id = ?", (registration_id,))
    state, cookie = start(server)
    await finish(server, state, cookie)
    assert conn.execute("SELECT last_used_at FROM registrations WHERE id = ?", (registration_id,)).fetchone()[0] == int(clock())


async def test_a_refused_connector_login_does_not(server, conn):
    registration_id = register(conn)
    conn.execute("UPDATE registrations SET last_used_at = 1 WHERE id = ?", (registration_id,))
    state, cookie = start(server)
    await finish(server, state, cookie, error="access_denied")
    assert conn.execute("SELECT last_used_at FROM registrations WHERE id = ?", (registration_id,)).fetchone()[0] == 1


async def test_after_leaving_the_connector_is_told_to_reconnect(server, conn):
    """PR 8d-2, through the shared public client grok.com uses. (An issued client is deleted on leaving, so its
    refresh ends at invalid_client before this.)"""
    from outer_steppes.oauth import MSG_CONNECTION_INVALID
    register(conn)
    body = await connect(server, conn)
    member = registry.active_registration(conn, CALLER)
    form = {"grant_type": "refresh_token", "refresh_token": body["refresh_token"], "client_id": PUBLIC}
    with transaction(conn):
        registry.revoke_in_tx(conn, member["id"], NOW)
    assert server.registration_for_access_token("Bearer " + body["access_token"]) is None
    result = server.token(form, None)
    assert (result.status, result.body) == (400, {"error": "invalid_grant", "error_description": MSG_CONNECTION_INVALID})


# ------------------------------------------------------------------ the connect-time warning (MALU-264)

async def test_the_warning_page_shows_the_warning_then_cancel_before_continue(http, conn):
    resp = await http.get("/oauth/authorize?" + urlencode(authorize_params()), allow_redirects=False)
    text = await resp.text()
    paragraphs = [f"<p>{html.escape(p)}</p>" for p in (*oauth.CONNECT_WARNING, oauth.MSG_CONNECT_NEXT)]
    positions = [text.index(p) for p in paragraphs]
    assert positions == sorted(positions)
    assert positions[-1] < text.index(">Cancel</a>") < text.index(">Continue</a>")  # Cancel first, as the main choice
    assert "class=main href" in text.split(">Cancel</a>")[0].rsplit("<a", 1)[1]
    assert "<form" not in text and "<script" not in text
    hrefs = re.findall(r"href='([^']*)'", text)
    assert len(hrefs) == 2 and not any(re.search(r"&(?!amp;)", href) for href in hrefs)  # HTML-escaped, every &
    assert page_link(text, "Cancel") == CALLBACK + "?error=access_denied&state=grok-state"
    assert page_link(text, "Continue").startswith("https://discord.com/oauth2/authorize?")


@pytest.mark.parametrize("hostile", ["a'b\"c", "x&code=injected", "<script>alert(1)</script>", "k=v&state=other"])
async def test_a_hostile_grok_state_comes_back_as_one_parameter_and_adds_none(http, conn, hostile):
    resp = await http.get("/oauth/authorize?" + urlencode(authorize_params(state=hostile)), allow_redirects=False)
    text = await resp.text()
    assert "<script>" not in text
    cancel = page_link(text, "Cancel")
    assert cancel.startswith(CALLBACK + "?")
    assert parse_qs(urlsplit(cancel).query) == {"error": ["access_denied"], "state": [hostile]}


async def test_the_account_login_still_redirects_at_once(http, conn):
    # Issued clients still answer with a code at once: tests/integration/clients.py, Connector.issued_code.
    resp = await http.get("/account/login", allow_redirects=False)
    assert resp.status == 302 and resp.headers["Location"].startswith("https://discord.com/oauth2/authorize?")


def test_the_connect_warning_equals_the_design_text_word_for_word():
    design = (Path(__file__).resolve().parents[1] / "DESIGN.md").read_text(encoding="utf-8")
    section = design.split("#### Connect-time warning", 1)[1]
    quoted = re.search(r"((?:^>.*\n)+)", section, re.MULTILINE).group(1)
    paragraphs = [line[2:] for line in quoted.splitlines() if line.startswith("> ")]
    assert tuple(paragraphs) == oauth.CONNECT_WARNING
    assert f'"{oauth.MSG_CONNECT_NEXT}"' in section.split("####", 1)[0]
