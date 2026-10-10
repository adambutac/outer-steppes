"""Personal by default, and the claim link (DESIGN.md, Members and automations > Claiming the bridge; ADR-0016)."""

from __future__ import annotations

import asyncio
import html
import logging
from dataclasses import replace
from urllib.parse import parse_qs, urlsplit

import pytest

from outer_steppes import claim, commands, metrics, registry, web_account
from outer_steppes.db import transaction
from outer_steppes.discord_login import DiscordLoginError, DiscordUser
from outer_steppes.http_app import create_public_app
from outer_steppes.interactions import InteractionHandler
from outer_steppes.loopback_api import create_admin_app
from outer_steppes.mcp_server import McpServer
from outer_steppes.oauth import OAuthServer
from outer_steppes.reply import HeldPrompts, ReplyService
from outer_steppes.web_account import AccountPages

from .conftest import (ADMIN_TOKEN, AUTOMATION_URL, CALLER, NOW, OWNER, SECRET, Clock, add_member, make_config,
                       page_link, public_resolver, register)

CLIENT_SECRET = "discord-oauth-client-secret-0123456789"  # made up; shaped like Discord's (no dots)
STRANGER = "777777777777777777"
BEARER = {"Authorization": f"Bearer {ADMIN_TOKEN}"}


class FakeIdentifier:
    """Returns `user`, or the user a code was made for in `by_code` (for logins that run at once)."""

    def __init__(self, user: DiscordUser = DiscordUser(OWNER, "owner"), fail: bool = False) -> None:
        self.user, self.fail, self.by_code = user, fail, {}

    async def identify(self, code: str) -> DiscordUser:
        if self.fail:
            raise DiscordLoginError("down")
        return self.by_code.get(code, self.user)


@pytest.fixture(autouse=True)
def fresh_metrics():
    return metrics.reset()


@pytest.fixture
def config(db_path):
    """The defaults with Discord login on: MEMBERSHIP=personal."""
    return make_config(database_path=db_path, discord_client_secret=CLIENT_SECRET, membership="personal")


class Bridge:
    """The admin and public listeners over one database, as the operator and a browser see them."""

    def __init__(self, admin, public, conn, clock, identifier):
        self.admin, self.public, self.conn, self.clock, self.identifier = admin, public, conn, clock, identifier

    async def mint(self) -> tuple[int, dict]:
        resp = await self.admin.post("/v1/claim", headers=BEARER)
        return resp.status, await resp.json() if resp.content_type == "application/json" else {}

    async def link(self) -> str:
        status, body = await self.mint()
        assert status == 200, body
        return body["claim_url"]

    async def open(self, link_or_path: str):
        """GET the claim link. Returns the response; a 302 carries the Discord state and the login cookie."""
        path = link_or_path.removeprefix("https://bridge.example")
        resp = await self.public.get(path, allow_redirects=False)
        await resp.read()
        self.public.session.cookie_jar.clear()
        return resp

    async def start(self, link: str) -> tuple[str, str]:
        resp = await self.open(link)
        assert resp.status == 302, await resp.text()
        state = parse_qs(urlsplit(resp.headers["Location"]).query)["state"][0]
        return state, resp.cookies["__Host-os_login"].value

    async def finish(self, state: str, nonce: str, user: DiscordUser | None = None, query: str = "code=c"):
        if user is not None:
            self.identifier.user = user
        resp = await self.public.get(f"/oauth/discord/callback?state={state}&{query}", allow_redirects=False,
                                     headers={"Cookie": f"__Host-os_login={nonce}"})
        await resp.read()
        self.public.session.cookie_jar.clear()
        return resp

    async def claim(self, user: DiscordUser | None = None):
        return await self.finish(*await self.start(await self.link()), user=user)

    async def login(self, path: str, user: DiscordUser):
        """A non-claim login: the account page's (path /account/login) or the connector's (an authorize URL)."""
        resp = await self.public.get(path, allow_redirects=False)
        text = await resp.text()
        self.public.session.cookie_jar.clear()
        if path == "/account/login":
            assert resp.status == 302, text
            discord = resp.headers["Location"]
        else:  # the connector stops on the connect-time warning; the member chooses Continue
            assert resp.status == 200 and "Location" not in resp.headers, text
            discord = page_link(text, "Continue")
        state = parse_qs(urlsplit(discord).query)["state"][0]
        return await self.finish(state, resp.cookies["__Host-os_login"].value, user=user)

    def members(self) -> list[str]:
        return [m["discord_user_id"] for m in registry.list_members(self.conn)]

    def settings(self) -> dict:
        return dict(self.conn.execute("SELECT key, value FROM settings"))


@pytest.fixture
async def make_bridge(aiohttp_client, conn, fake_discord):
    async def build(config, clock=None):
        clock = clock or Clock()
        identifier = FakeIdentifier()
        oauth_server = OAuthServer(conn, config, clock)
        replies = ReplyService(conn, fake_discord, config, lambda: None, HeldPrompts(), clock=clock)
        handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=clock)
        pages = AccountPages(conn, config, lambda: None, resolver=public_resolver, clock=clock)
        public = create_public_app(config, handler, lambda *_: None, clock=clock, oauth=oauth_server, mcp=McpServer(oauth_server, replies),
                                   discord_login=identifier, account=pages)
        admin = create_admin_app(config, conn, lambda: None, clock=clock, resolver=public_resolver)
        return Bridge(await aiohttp_client(admin), await aiohttp_client(public), conn, clock, identifier)
    return build


@pytest.fixture
async def bridge(make_bridge, config):
    return await make_bridge(config)


CONNECTOR_AUTHORIZE = ("/oauth/authorize?response_type=code&client_id=outer-steppes"
                       "&redirect_uri=https://grok.com/connectors-oauth-exchange-code/&state=g"
                       "&code_challenge=" + "c" * 43 + "&code_challenge_method=S256")


# ------------------------------------------------------------------ minting (POST /v1/claim)

async def test_minting_returns_the_link_once_and_stores_only_its_hash(bridge, caplog):
    caplog.set_level(logging.DEBUG)
    status, body = await bridge.mint()
    assert status == 200 and body["expires_at"] == NOW + 3600
    prefix = "https://bridge.example/account/claim?token="
    assert body["claim_url"].startswith(prefix)
    token = body["claim_url"][len(prefix):]
    assert len(token) >= 43  # 256 bits, URL-safe
    assert bridge.settings() == {"claim_hash": claim.secret_hash(token), "claim_expires_at": str(NOW + 3600)}
    dump = "\n".join(bridge.conn.iterdump())
    assert token not in dump  # only the hash is stored
    assert token not in "\n".join(r.getMessage() for r in caplog.records)  # and never logged


async def test_a_new_link_replaces_the_old_one(bridge):
    old, new = await bridge.link(), await bridge.link()
    assert old != new
    assert (await bridge.open(old)).status == 400
    assert (await bridge.open(new)).status == 302


async def test_minting_needs_the_bearer(bridge):
    resp = await bridge.admin.post("/v1/claim")
    assert resp.status == 401 and bridge.settings() == {}


@pytest.mark.parametrize("setup,status,error", [
    ("claimed", 409, "claimed"),
    ("operator", 409, "operator_mode"),
    ("login_off", 404, "discord_login_off"),
    ("paused", 403, None),
])
async def test_minting_refusals_store_nothing(make_bridge, config, conn, setup, status, error):
    if setup == "claimed":
        add_member(conn, OWNER)
    if setup == "paused":
        conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    overrides = {"operator": {"membership": "operator"},
                 "login_off": {"membership": "operator", "discord_client_secret": ""}}.get(setup, {})
    bridge = await make_bridge(replace(config, **overrides))
    got, body = await bridge.mint()
    assert got == status and body.get("error") == error
    assert "claim_hash" not in bridge.settings()


# ------------------------------------------------------------------ GET /account/claim

async def test_opening_the_link_starts_a_claim_login_without_leaking_the_token(bridge):
    link = await bridge.link()
    resp = await bridge.open(link)
    assert resp.status == 302 and resp.headers["Location"].startswith("https://discord.com/oauth2/authorize?")
    token = link.split("token=", 1)[1]
    assert token not in resp.headers["Location"]
    assert resp.headers["Referrer-Policy"] == "no-referrer"  # the Referer to discord.com carries no token
    row = bridge.conn.execute("SELECT purpose, claim_hash FROM oauth_pending").fetchone()
    assert (row["purpose"], row["claim_hash"]) == ("claim", claim.secret_hash(token))
    assert bridge.members() == [] and "claim_hash" in bridge.settings()  # opening it uses nothing up


@pytest.mark.parametrize("path", [
    "/account/claim",
    "/account/claim?token=",
    "/account/claim?token=not-the-token",
])
async def test_a_wrong_or_missing_token_writes_nothing(bridge, path):
    await bridge.link()
    resp = await bridge.open(path)
    text = await resp.text()
    assert resp.status == 400 and "This claim link is invalid, expired, or was already used." in text
    assert "`" not in text  # pages render no markdown
    assert bridge.conn.execute("SELECT count(*) FROM oauth_pending").fetchone()[0] == 0


async def test_an_expired_link_is_refused(bridge):
    link = await bridge.link()
    bridge.clock.now = NOW + claim.CLAIM_TTL_SECONDS
    assert (await bridge.open(link)).status == 400


async def test_a_repeated_token_is_refused(bridge):
    token = (await bridge.link()).split("token=", 1)[1]
    resp = await bridge.open(f"/account/claim?token={token}&token={token}")
    assert resp.status == 400 and bridge.conn.execute("SELECT count(*) FROM oauth_pending").fetchone()[0] == 0


async def test_head_starts_nothing(bridge):
    link = await bridge.link()
    resp = await bridge.public.head(link.removeprefix("https://bridge.example"), allow_redirects=False)
    assert resp.status == 405 and bridge.conn.execute("SELECT count(*) FROM oauth_pending").fetchone()[0] == 0


async def test_a_live_link_on_a_claimed_bridge_is_refused(bridge):
    link = await bridge.link()
    bridge.conn.execute("INSERT INTO registrations (id, discord_user_id, created_at, updated_at) "
                        "VALUES ('x', ?, 0, 0)", (OWNER,))  # by hand, so the claim secret stays behind
    assert (await bridge.open(link)).status == 400


async def test_a_leftover_link_is_refused_with_membership_operator(make_bridge, config, conn):
    with transaction(conn):
        token, _ = claim.mint_in_tx(conn, NOW)
    bridge = await make_bridge(replace(config, membership="operator"))
    assert (await bridge.open(f"/account/claim?token={token}")).status == 400


async def test_with_discord_login_off_the_claim_page_is_unavailable(make_bridge, config):
    bridge = await make_bridge(replace(config, membership="operator", discord_client_secret=""))
    resp = await bridge.open("/account/claim?token=x")
    assert resp.status == 404 and html.escape(web_account.MSG_UNAVAILABLE) in await resp.text()


# ------------------------------------------------------------------ the callback

async def test_claiming_makes_the_only_member_and_uses_the_link_up(bridge):
    link = await bridge.link()
    resp = await bridge.finish(*await bridge.start(link))
    assert (resp.status, resp.headers["Location"]) == (303, "https://bridge.example/account")
    session = resp.cookies["__Host-os_session"].value
    assert bridge.members() == [OWNER] and "claim_hash" not in bridge.settings()
    assert metrics.METRICS.discord_logins.value("claim", "claimed") == 1
    assert metrics.METRICS.membership_changes.value("self", "claimed") == 1
    page = await bridge.public.get("/account", headers={"Cookie": f"__Host-os_session={session}"})
    assert "Signed in as <b>owner</b>" in await page.text() and "name=signing_secret" in await page.text()
    assert (await bridge.open(link)).status == 400  # used up
    assert (await bridge.mint())[1] == {"error": "claimed"}


async def test_a_link_replaced_mid_login_fails(bridge):
    state, nonce = await bridge.start(await bridge.link())
    await bridge.link()
    resp = await bridge.finish(state, nonce)
    assert resp.status == 403 and "this claim link is no longer valid" in await resp.text()
    assert bridge.members() == [] and "__Host-os_session" not in resp.cookies
    assert metrics.METRICS.discord_logins.value("claim", "claim_invalid") == 1


async def test_a_link_that_expires_mid_login_fails(bridge):
    link = await bridge.link()
    bridge.clock.now = NOW + claim.CLAIM_TTL_SECONDS - 60
    state, nonce = await bridge.start(link)
    bridge.clock.now = NOW + claim.CLAIM_TTL_SECONDS  # the pending login itself is still live
    assert (await bridge.finish(state, nonce)).status == 403 and bridge.members() == []


async def test_one_link_makes_one_owner_even_when_two_people_race(bridge):
    link = await bridge.link()
    first, second = await bridge.start(link), await bridge.start(link)
    results = []
    for (state, nonce), user in ((first, DiscordUser(OWNER, "owner")), (second, DiscordUser(STRANGER, "x"))):
        results.append((await bridge.finish(state, nonce, user=user)).status)
    assert results == [303, 403] and bridge.members() == [OWNER]


async def test_concurrent_claims_make_one_member(bridge):
    link = await bridge.link()
    logins = [await bridge.start(link), await bridge.start(link)]
    bridge.identifier.by_code = {"a": DiscordUser(OWNER, "a"), "b": DiscordUser(STRANGER, "b")}
    results = await asyncio.gather(*(bridge.finish(*login, query=f"code={code}")
                                     for login, code in zip(logins, "ab")))
    assert sorted(r.status for r in results) == [303, 403] and len(bridge.members()) == 1


async def test_the_allowlist_applies_to_claiming(make_bridge, config):
    bridge = await make_bridge(replace(config, user_allowlist=frozenset({STRANGER})))
    resp = await bridge.claim()
    assert resp.status == 403 and "isn&#x27;t allowed here" in await resp.text()
    assert bridge.members() == [] and "claim_hash" in bridge.settings()  # the link still works for someone listed


async def test_no_claim_while_paused(bridge):
    state, nonce = await bridge.start(await bridge.link())
    bridge.conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    resp = await bridge.finish(state, nonce)
    assert resp.status == 403 and "paused" in await resp.text() and bridge.members() == []


async def test_membership_operator_at_the_callback_refuses(make_bridge, config, conn):
    personal = await make_bridge(config)
    state, nonce = await personal.start(await personal.link())
    operator = await make_bridge(replace(config, membership="operator"))
    assert (await operator.finish(state, nonce)).status == 403 and operator.members() == []


async def test_an_existing_member_just_signs_in(bridge):
    state, nonce = await bridge.start(await bridge.link())
    with transaction(bridge.conn):
        registry.get_or_create_member_in_tx(bridge.conn, OWNER, NOW)  # the operator added them meanwhile
    resp = await bridge.finish(state, nonce)
    assert resp.status == 303 and "__Host-os_session" in resp.cookies and bridge.members() == [OWNER]
    assert metrics.METRICS.discord_logins.value("claim", "ok") == 1


@pytest.mark.parametrize("query,status,result", [("error=access_denied", 400, "denied"),
                                                 ("code=c", 503, "discord_error")])
async def test_cancelled_or_discord_down_leaves_the_link_live(bridge, query, status, result):
    state, nonce = await bridge.start(await bridge.link())
    bridge.identifier.fail = True
    assert (await bridge.finish(state, nonce, query=query)).status == status
    assert bridge.members() == [] and "claim_hash" in bridge.settings()
    assert metrics.METRICS.discord_logins.value("claim", result) == 1


# ------------------------------------------------------------------ who may join

@pytest.mark.parametrize("membership", ["personal", "shared"])
@pytest.mark.parametrize("path", [CONNECTOR_AUTHORIZE, "/account/login"])
async def test_nobody_joins_an_unclaimed_bridge_by_signing_in(make_bridge, config, membership, path):
    bridge = await make_bridge(replace(config, membership=membership))
    resp = await bridge.login(path, DiscordUser(STRANGER, "stranger"))
    text = await resp.text()
    assert resp.status == 403 and "it hasn&#x27;t been claimed yet" in text and "`" not in text
    assert bridge.members() == []
    purpose = "register" if path == "/account/login" else "connector"
    assert metrics.METRICS.discord_logins.value(purpose, "unclaimed") == 1


@pytest.mark.parametrize("path", [CONNECTOR_AUTHORIZE, "/account/login"])
async def test_a_claimed_personal_bridge_takes_nobody_else(bridge, path):
    assert (await bridge.claim()).status == 303
    resp = await bridge.login(path, DiscordUser(STRANGER, "stranger"))
    assert resp.status == 403 and "it&#x27;s personal to its owner" in await resp.text()
    assert bridge.members() == [OWNER]
    # The owner still signs in.
    assert (await bridge.login(path, DiscordUser(OWNER, "owner"))).status in (302, 303)


async def test_a_claimed_shared_bridge_takes_others(make_bridge, config):
    bridge = await make_bridge(replace(config, membership="shared"))
    assert (await bridge.claim()).status == 303
    resp = await bridge.login("/account/login", DiscordUser(STRANGER, "stranger"))
    assert resp.status == 303 and sorted(bridge.members()) == sorted([OWNER, STRANGER])


async def test_a_bridge_whose_last_member_leaves_is_unclaimed_again(bridge):
    assert (await bridge.claim()).status == 303
    with transaction(bridge.conn):
        registry.revoke_in_tx(bridge.conn, registry.active_registration(bridge.conn, OWNER)["id"], NOW)
    resp = await bridge.login("/account/login", DiscordUser(OWNER, "owner"))
    assert resp.status == 403 and "hasn&#x27;t been claimed yet" in await resp.text()
    assert (await bridge.claim()).status == 303 and bridge.members() == [OWNER]  # a new link brings them back


async def test_the_operator_adding_a_member_claims_the_bridge(bridge):
    link = await bridge.link()
    resp = await bridge.admin.post("/v1/registrations", headers=BEARER, json={
        "discord_user_id": CALLER, "automation_url": AUTOMATION_URL, "signing_secret": SECRET})
    assert resp.status == 200
    assert "claim_hash" not in bridge.settings() and (await bridge.open(link)).status == 400
    assert (await bridge.mint())[1] == {"error": "claimed"}


# ------------------------------------------------------------------ the wording, by MEMBERSHIP

def test_not_a_member_text_follows_membership(conn):
    assert registry.resolve_route(conn, CALLER, "personal").message == registry.MSG_NOT_MEMBER_PERSONAL
    assert "osctl bridge claim" in registry.MSG_NOT_MEMBER_PERSONAL
    assert registry.resolve_route(conn, CALLER, "shared").message == registry.MSG_NOT_MEMBER
    assert registry.resolve_route(conn, CALLER, "operator").message == registry.MSG_NOT_MEMBER_OPERATOR


def test_webhook_set_links_to_the_page_unless_operator():
    assert "/account" in commands.webhook_set("https://bridge.example", "personal").message
    assert "/account" in commands.webhook_set("https://bridge.example", "shared").message
    assert commands.webhook_set("https://bridge.example", "operator").message == commands.MSG_WEBHOOK_OPERATOR_ONLY


@pytest.mark.parametrize("membership,text", [
    ("shared", "You can join again later by connecting grok.com or signing in here."),
    ("personal", "Joining again needs a new claim link or the operator."),
    ("operator", "Joining again needs the operator."),
])
async def test_the_leave_section_says_how_to_join_again(make_bridge, config, conn, membership, text):
    register(conn, OWNER)
    bridge = await make_bridge(replace(config, membership=membership))
    resp = await bridge.login("/account/login", DiscordUser(OWNER, "owner"))
    page = await bridge.public.get("/account", headers={
        "Cookie": f"__Host-os_session={resp.cookies['__Host-os_session'].value}"})
    assert text in await page.text()
