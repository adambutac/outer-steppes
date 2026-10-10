"""The account page (DESIGN.md, The account page)."""

from __future__ import annotations

import base64
import hashlib
import html
import logging
import re
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest

from outer_steppes import foreign, http_app, metrics, oauth, registry, sessions, web_account
from outer_steppes.db import transaction
from outer_steppes.discord_login import DiscordLoginError, DiscordUser
from outer_steppes.http_app import create_public_app
from outer_steppes.interactions import InteractionHandler
from outer_steppes.loopback_api import create_admin_app
from outer_steppes.mcp_server import McpServer
from outer_steppes.oauth import OAuthServer
from outer_steppes.reply import HeldPrompts, ReplyService
from outer_steppes.sweeper import apply_retention
from outer_steppes.web_account import AccountPages

from .conftest import (ADMIN_TOKEN, AUTOMATION_URL, CALLER, NOW, OWNER, SECRET, Clock, add_member,
                       automation_url_for, claim_bridge, command, make_config, public_resolver, register, snowflake)

REPO = Path(__file__).resolve().parents[1]
ORIGIN = "https://bridge.example"
CLIENT_SECRET = "discord-oauth-client-secret-0123456789"  # made up; shaped like Discord's (no dots)
OTHER_URL = "https://grok.com/webhook/automation/00000000-0000-4000-8000-000000000002"
# Made up, and built at runtime so no whsec_ literal is committed (gitleaks).
OTHER_SECRET = "whsec_" + base64.b64encode(b"fedcba9876543210").decode()
COOKIE = sessions.SESSION_COOKIE


class FakeIdentifier:
    def __init__(self, user: DiscordUser = DiscordUser(CALLER, "member"), fail: bool = False) -> None:
        self.user, self.fail = user, fail

    async def identify(self, code: str) -> DiscordUser:
        if self.fail:
            raise DiscordLoginError("down")
        return self.user


@pytest.fixture(autouse=True)
def fresh_metrics():
    return metrics.reset()


@pytest.fixture
def config(db_path):
    return make_config(database_path=db_path, discord_client_secret=CLIENT_SECRET, membership="shared")


class Harness:
    def __init__(self, http, conn, config, clock, identifier, wakes):
        self.http, self.conn, self.config, self.clock, self.identifier, self.wakes = (
            http, conn, config, clock, identifier, wakes)
        self.last = None

    async def _send(self, method, path, **kwargs):
        resp = await self.http.request(method, path, allow_redirects=False, **kwargs)
        await resp.read()
        # Cookies are sent by hand, so the client's jar never adds one on its own.
        self.http.session.cookie_jar.clear()
        return resp

    async def start_login(self):
        """GET /account/login. Returns (discord state, __Host-os_login nonce)."""
        resp = await self._send("GET", "/account/login")
        assert resp.status == 302 and resp.headers["Location"].startswith("https://discord.com/oauth2/authorize?")
        state = parse_qs(urlsplit(resp.headers["Location"]).query)["state"][0]
        return state, resp.cookies["__Host-os_login"].value

    async def callback(self, state, nonce, query="code=c"):
        self.last = await self._send("GET", f"/oauth/discord/callback?state={state}&{query}",
                                     headers={"Cookie": f"__Host-os_login={nonce}"})
        return self.last

    async def login(self, user: DiscordUser | None = None):
        """A full account-page login. Returns the session cookie value, or None if refused."""
        if user is not None:
            self.identifier.user = user
        resp = await self.callback(*await self.start_login())
        return resp.cookies[COOKIE].value if COOKIE in resp.cookies else None

    async def page(self, session=None, query=""):
        headers = {"Cookie": f"{COOKIE}={session}"} if session else {}
        return await self._send("GET", "/account" + query, headers=headers)

    async def post(self, path, session, data=None, origin=ORIGIN, csrf=None):
        form = dict(data or {})
        form.setdefault("csrf", csrf if csrf is not None else (sessions.csrf_for(session) if session else ""))
        headers = {"Origin": origin} if origin else {}
        if session:
            headers["Cookie"] = f"{COOKIE}={session}"
        return await self._send("POST", path, data=form, headers=headers)

    async def save(self, session, url=AUTOMATION_URL, secret=SECRET, **kwargs):
        return await self.post("/account/webhook", session, {"automation_url": url, "signing_secret": secret},
                               **kwargs)

    def member(self, user_id=CALLER):
        return registry.active_registration(self.conn, user_id)

    def automation(self, user_id=CALLER):
        member = self.member(user_id)
        return registry.active_automation(self.conn, member["id"]) if member else None

    def count(self, table):
        return self.conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]


@pytest.fixture
async def make_harness(aiohttp_client, conn, fake_discord):
    async def build(config, identifier=None, clock=None, claimed=True):
        """`claimed`: start from a bridge its owner has claimed, so that others can join a shared one."""
        if claimed and not registry.active_registration(conn, OWNER):
            with transaction(conn):
                claim_bridge(conn)
        clock = clock or Clock()
        wakes = []
        oauth_server = OAuthServer(conn, config, clock)
        replies = ReplyService(conn, fake_discord, config, lambda: None, HeldPrompts(), clock=clock)
        handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=clock)
        identifier = identifier or FakeIdentifier()
        pages = AccountPages(conn, config, lambda: wakes.append(1), resolver=public_resolver, clock=clock)
        app = create_public_app(config, handler, lambda *_: None, clock=clock, oauth=oauth_server, mcp=McpServer(oauth_server, replies),
                                discord_login=identifier, account=pages)
        return Harness(await aiohttp_client(app), conn, config, clock, identifier, wakes)
    return build


@pytest.fixture
async def h(make_harness, config, clock):
    return await make_harness(config, clock=clock)


def posted_for(h):
    """A posted /ask for the signed-in member's current automation."""
    iid = snowflake()
    h.conn.execute("INSERT INTO interactions (interaction_id, status, ack_kind, registration_id, automation_id, "
                   "interaction_token, created_at, updated_at) VALUES (?, 'posted', 'defer', ?, ?, 'tok', ?, ?)",
                   (iid, h.member()["id"], h.automation()["id"], NOW, NOW))
    return iid


def pause(conn):
    with transaction(conn):
        registry.set_paused_in_tx(conn, True, NOW)


# ------------------------------------------------------------------ sign-in

async def test_login_creates_the_member_sets_the_host_cookie_and_returns_to_account(h):
    state, _ = await h.start_login()
    row = h.conn.execute("SELECT * FROM oauth_pending").fetchone()
    assert (row["purpose"], row["redirect_uri"], row["code_challenge"]) == ("register", None, None)
    assert row["state_hash"] == oauth._hash(state)

    session = await h.login()
    assert h.last.status == 303 and h.last.headers["Location"] == ORIGIN + "/account"
    cookie = next(c for c in h.last.headers.getall("Set-Cookie") if c.startswith(COOKIE + "="))
    attributes = dict((part.strip().split("=", 1) + [""])[:2] for part in cookie.split(";")[1:])
    attributes = {key.lower(): value for key, value in attributes.items()}
    # Exactly these, and no Domain: what the __Host- prefix requires.
    assert attributes == {"httponly": "", "max-age": "1800", "path": "/", "samesite": "Lax", "secure": ""}
    assert COOKIE == "__Host-os_session"
    assert h.member() is not None and h.automation() is None
    stored = h.conn.execute("SELECT * FROM web_sessions").fetchone()
    assert stored["session_hash"] == oauth._hash(session) and session not in str(tuple(stored))
    assert (stored["discord_user_id"], stored["username"], stored["expires_at"]) == (CALLER, "member", NOW + 1800)
    assert metrics.METRICS.discord_logins.value("register", "ok") == 1
    assert metrics.METRICS.membership_changes.value("self", "joined") == 1


async def test_login_by_an_existing_member_keeps_the_membership(h):
    registration_id = register(h.conn)
    assert await h.login()
    assert h.member()["id"] == registration_id and h.automation()["id"] == registration_id
    assert metrics.METRICS.membership_changes.value("self", "joined") == 0


async def test_get_account_without_a_session_is_static_and_writes_nothing(h):
    resp = await h.page()
    body = await resp.text()
    assert resp.status == 200 and "Sign in with Discord" in body and ORIGIN + "/account/login" in body
    assert h.count("oauth_pending") == 0 and "Set-Cookie" not in resp.headers
    resp = await h.page(session="not-a-session")
    assert "Sign in with Discord" in await resp.text() and h.count("oauth_pending") == 0


async def test_head_account_login_is_refused_and_writes_nothing(h):
    resp = await h._send("HEAD", "/account/login")
    assert resp.status == 405 and h.count("oauth_pending") == 0


async def test_cancelled_and_discord_down_pages_link_back_to_account(h):
    resp = await h.callback(*await h.start_login(), query="error=access_denied")
    assert resp.status == 400 and "Sign-in cancelled." in await resp.text()
    assert f"href='{ORIGIN}/account'" in await resp.text()
    h.identifier.fail = True
    resp = await h.callback(*await h.start_login())
    assert resp.status == 503 and "Discord didn&#x27;t answer; try again." in await resp.text()
    assert h.member() is None and h.count("web_sessions") == 0
    assert metrics.METRICS.discord_logins.value("register", "denied") == 1
    assert metrics.METRICS.discord_logins.value("register", "discord_error") == 1


async def test_bad_state_or_missing_cookie_gets_the_bad_state_page(h):
    state, _ = await h.start_login()
    resp = await h.callback(state, "wrong-nonce")
    assert resp.status == 400 and h.count("web_sessions") == 0
    assert metrics.METRICS.discord_logins.value("register", "bad_state") == 1


@pytest.mark.parametrize("member", [True, False])
async def test_login_while_paused(h, member):
    """An existing member signs in, so removing and leaving stay reachable; nobody new joins."""
    if member:
        register(h.conn)
    pause(h.conn)
    session = await h.login()
    if member:
        assert session and h.last.status == 303
    else:
        assert session is None and h.last.status == 403 and "paused" in await h.last.text()
        assert h.member() is None and h.count("web_sessions") == 0
        assert metrics.METRICS.discord_logins.value("register", "paused") == 1


@pytest.mark.parametrize("overrides,result", [
    ({"max_members": 1}, "full"),
    ({"user_allowlist": frozenset({"999999999999999999"})}, "not_allowed"),
    ({"membership": "operator"}, "not_registered"),
])
async def test_login_refusals_write_nothing(make_harness, config, conn, overrides, result):
    if result == "full":
        add_member(conn, "999999999999999999")
    h = await make_harness(replace(config, **overrides))
    assert await h.login() is None and h.last.status == 403
    assert h.member() is None and h.count("web_sessions") == 0
    assert metrics.METRICS.discord_logins.value("register", result) == 1


async def test_listed_out_member_cannot_sign_in(make_harness, config, conn):
    register(conn)
    h = await make_harness(replace(config, user_allowlist=frozenset({"999999999999999999"})))
    assert await h.login() is None and h.last.status == 403


async def test_username_is_escaped(h):
    session = await h.login(DiscordUser(CALLER, "<script>alert(1)</script>"))
    body = await (await h.page(session)).text()
    assert "<script>alert(1)</script>" not in body and "&lt;script&gt;" in body


async def test_account_page_is_unavailable_without_discord_login(make_harness, config):
    h = await make_harness(replace(config, discord_client_secret="", membership="operator"))
    for resp in (await h.page(), await h._send("GET", "/account/login")):
        assert resp.status == 404 and "isn&#x27;t available" in await resp.text()
    assert h.count("oauth_pending") == 0


# ------------------------------------------------------------------ the page

async def test_page_shows_state_but_never_the_url_or_secret(h):
    session = await h.login()
    body = await (await h.page(session)).text()
    assert "Signed in as <b>member</b>" in body and "Not set." in body and "Remove automation" not in body
    assert "type=password" in body and "autocomplete=off" in body

    assert (await h.save(session)).status == 303
    body = await (await h.page(session, "?done=saved")).text()
    assert "Set on 2023-11-14." in body and "Saved. Your automation is set." in body
    assert "Remove automation" in body
    assert AUTOMATION_URL not in body and SECRET not in body and SECRET[6:] not in body


async def test_page_shows_the_connector_values_and_the_exact_instructions(h):
    body = await (await h.page(await h.login())).text()
    for value in (ORIGIN + "/mcp", ORIGIN + "/oauth/authorize", ORIGIN + "/oauth/token", "outer-steppes",
                  "(leave empty)", "none"):
        assert f"<code>{html.escape(value)}</code>" in body
    assert f"<pre>{html.escape(web_account.INSTRUCTIONS)}</pre>" in body
    assert "integration_type=1" in body and f"<code>{ORIGIN}</code>" in body


def test_instructions_equal_the_design_text_word_for_word():
    design = (REPO / "DESIGN.md").read_text(encoding="utf-8")
    section = design.split("**MCP delivery**", 1)[1]
    quoted = re.search(r"((?:^>.*\n)+)", section, re.MULTILINE).group(1)
    text = "\n".join(line[2:] if line.startswith("> ") else line[1:] for line in quoted.splitlines())
    assert text == web_account.INSTRUCTIONS


async def test_the_operator_warning_comes_directly_before_the_save_form(h):
    warning = f"<p>{html.escape(web_account.MSG_OPERATOR_TRUST)}</p>"
    session = await h.login()
    body = await (await h.page(session)).text()
    assert body.count(warning) == 1
    assert body.index(warning) + len(warning) == body.index(f"<form method=post action='{ORIGIN}/account/webhook'>")

    # A refused save re-renders the page, and the warning is still above the form.
    response = await h.save(session, url="https://example.com/not-grok")
    assert response.status == 400 and warning in await response.text()


def test_the_operator_warning_equals_the_design_text_word_for_word():
    design = (REPO / "DESIGN.md").read_text(encoding="utf-8")
    section = design.split("### Operator warning", 1)[1]
    quoted = re.search(r"^> (.*)$", section, re.MULTILINE).group(1)
    assert quoted == web_account.MSG_OPERATOR_TRUST


async def test_unknown_notice_is_not_echoed(h):
    body = await (await h.page(await h.login(), "?done=%3Cb%3Ehi")).text()
    assert "hi" not in body.split("Signed in as")[1].split("<h2>")[0]


async def test_account_pages_carry_same_origin_referrer_and_form_action_self(h):
    session = await h.login()
    for resp in (await h.page(), await h.page(session), await h.save(session), await h.post(
            "/account/webhook", None)):
        assert resp.headers["Referrer-Policy"] == "same-origin"
        assert "form-action 'self';" in resp.headers["Content-Security-Policy"]
        assert resp.headers["Cache-Control"] == "no-store"
    # Every other page keeps no-referrer.
    resp = await h._send("GET", "/oauth/discord/callback?state=x&code=y")
    assert resp.headers["Referrer-Policy"] == "no-referrer"


# ------------------------------------------------------------------ saving

async def test_save_creates_then_replaces_through_the_loopback_validation(h):
    session = await h.login()
    resp = await h.save(session)
    assert resp.status == 303 and resp.headers["Location"] == ORIGIN + "/account?done=saved"
    first = h.automation()
    assert (first["automation_url"], first["signing_secret"]) == (AUTOMATION_URL, SECRET)
    iid = posted_for(h)

    resp = await h.save(session, url=OTHER_URL, secret=OTHER_SECRET)
    second = h.automation()
    assert resp.status == 303 and second["id"] != first["id"] and second["automation_url"] == OTHER_URL
    assert h.conn.execute("SELECT status FROM interactions WHERE interaction_id = ?", (iid,)).fetchone()[0] == "failed"
    assert h.wakes == [1]
    assert metrics.METRICS.membership_changes.value("self", "webhook_set") == 1
    assert metrics.METRICS.membership_changes.value("self", "webhook_replaced") == 1


@pytest.mark.parametrize("url,secret,result", [
    ("https://evil.example/webhook/automation/00000000-0000-4000-8000-000000000001", SECRET, "url_rejected"),
    (AUTOMATION_URL, "whsec_c2hvcnQ=", "secret_rejected"),
    (AUTOMATION_URL, "not-a-secret-at-all", "secret_rejected"),
])
async def test_bad_url_or_secret_re_renders_without_echoing(h, url, secret, result):
    session = await h.login()
    resp = await h.save(session, url=url, secret=secret)
    body = await resp.text()
    assert resp.status == 400 and "class=error" in body and "Trigger URL" in body
    assert url not in body and secret not in body
    assert h.automation() is None and h.count("account_changes") == 0
    assert metrics.METRICS.membership_changes.value("self", result) == 1


async def test_post_refusals_write_nothing(h, clock):
    session = await h.login()
    other = await h.login(DiscordUser("555555555555555555", "other"))
    refusals = [
        await h.save(None),                                   # no session
        await h.save(session, csrf="0" * 64),                 # wrong CSRF token
        await h.save(session, csrf=sessions.csrf_for(other)),  # another session's token
        await h.save(session, csrf=""),                       # missing token
        await h.save(session, origin=None),                   # missing Origin
        await h.save(session, origin="https://evil.example"),  # foreign Origin
        await h.save(session, origin="null"),                 # what no-referrer would send
        await h.post("/account/webhook", session, {"automation_url": AUTOMATION_URL, "signing_secret": SECRET,
                                                   "pad": "x" * 9000}),  # over 8 KB
    ]
    assert [r.status for r in refusals] == [403, 403, 403, 403, 403, 403, 403, 413]
    assert h.automation() is None and h.count("account_changes") == 0

    clock.now = NOW + sessions.SESSION_TTL_SECONDS  # the expiry doesn't slide
    assert (await h.save(session)).status == 403 and h.automation() is None


async def test_repeated_fields_and_non_form_bodies_are_refused(h):
    session = await h.login()
    headers = {"Origin": ORIGIN, "Cookie": f"{COOKIE}={session}"}
    csrf = sessions.csrf_for(session)
    resp = await h._send("POST", "/account/webhook", headers={**headers, "Content-Type":
                         "application/x-www-form-urlencoded"},
                         data=f"csrf={csrf}&csrf={csrf}&automation_url=a&signing_secret=b")
    assert resp.status == 400
    resp = await h._send("POST", "/account/webhook", headers=headers, json={"csrf": csrf})
    assert resp.status == 400 and h.automation() is None


@pytest.mark.parametrize("event", ["pause", "leave"])
async def test_pause_or_leaving_during_the_dns_lookup_refuses_the_save(make_harness, config, conn, clock, event):
    """DNS runs outside the transaction, so the write re-checks pause and membership."""
    h = await make_harness(config, clock=clock)
    session = await h.login()

    async def resolver(host):
        if event == "pause":
            pause(conn)
        else:
            conn.execute("UPDATE registrations SET revoked_at = ? WHERE discord_user_id = ?", (NOW, CALLER))
        return await public_resolver(host)

    h.http.app[http_app.ACCOUNT_KEY]._resolver = resolver
    resp = await h.save(session)
    assert resp.status == (503 if event == "pause" else 403)
    assert h.count("automations") == 0 and h.count("account_changes") == 0


async def test_save_while_paused_is_refused(h):
    session = await h.login()
    pause(h.conn)
    assert (await h.save(session)).status == 503 and h.automation() is None


async def test_allowlist_and_membership_are_rechecked_on_save(make_harness, config, h):
    session = await h.login()
    listed_out = await make_harness(replace(config, user_allowlist=frozenset({"999999999999999999"})))
    assert (await listed_out.save(session)).status == 403
    operator_only = await make_harness(replace(config, membership="operator"))
    assert (await operator_only.save(session)).status == 403
    assert h.automation() is None


async def test_eleventh_save_within_an_hour_is_refused(h, clock):
    session = await h.login()
    for _ in range(sessions.MAX_CHANGES_PER_WINDOW):
        assert (await h.save(session)).status == 303
    kept = h.automation()["id"]
    assert (await h.save(session)).status == 429 and h.automation()["id"] == kept
    assert metrics.METRICS.membership_changes.value("self", "rate_limited") == 1
    clock.now = NOW + sessions.CHANGE_WINDOW_SECONDS
    session = await h.login()
    assert (await h.save(session)).status == 303


async def test_with_membership_operator_a_member_can_remove_and_leave_but_not_save(make_harness, config, conn):
    register(conn)
    h = await make_harness(replace(config, membership="operator"))
    session = await h.login()
    body = await (await h.page(session)).text()
    assert "Trigger URL" not in body and "only the operator sets automations" in body
    assert html.escape(web_account.MSG_OPERATOR_TRUST) not in body
    assert (await h.save(session)).status == 403
    assert (await h.post("/account/webhook/remove", session)).status == 303 and h.automation() is None
    assert (await h.post("/account/leave", session)).status == 303 and h.member() is None


# ------------------------------------------------------------------ remove, leave, sign out

async def test_remove_keeps_the_member_and_connector_works_while_paused_and_is_not_rate_limited(h):
    session = await h.login()
    for _ in range(sessions.MAX_CHANGES_PER_WINDOW):
        await h.save(session)
    h.conn.execute("INSERT INTO oauth_tokens (token_hash, kind, client_id, registration_id, connection_id, "
                   "connected_at, expires_at, created_at) VALUES ('t', 'refresh', 'outer-steppes', ?, 'c', ?, ?, ?)",
                   (h.member()["id"], NOW, NOW + 60, NOW))
    iid = posted_for(h)
    resp = await h.post("/account/webhook/remove", session)
    assert resp.status == 303 and resp.headers["Location"] == ORIGIN + "/account?done=removed"
    assert h.automation() is None and h.member() is not None and h.count("oauth_tokens") == 1
    assert h.conn.execute("SELECT status FROM interactions WHERE interaction_id = ?", (iid,)).fetchone()[0] == "failed"
    assert h.wakes == [1] and metrics.METRICS.membership_changes.value("self", "webhook_removed") == 1
    body = await (await h.page(session, "?done=removed")).text()
    assert "this doesn&#x27;t disconnect grok.com&#x27;s connector." in body and "connected" not in body

    await h.save(session)  # rate-limited: removing didn't count, and the save is refused
    assert h.automation() is None
    pause(h.conn)
    assert (await h.post("/account/webhook/remove", session)).status == 303  # idempotent, and works while paused
    assert metrics.METRICS.membership_changes.value("self", "webhook_removed") == 1


async def test_leave_matches_revoke_and_ends_every_session(h):
    session = await h.login()
    second = await h.login()
    await h.save(session)
    iid = posted_for(h)
    resp = await h.post("/account/leave", session)
    assert resp.status == 303 and resp.headers["Location"] == ORIGIN + "/account?done=left"
    assert any(c.startswith(COOKIE + '=""') for c in resp.headers.getall("Set-Cookie"))
    assert h.member() is None and h.count("web_sessions") == 0
    assert h.conn.execute("SELECT count(*) FROM automations WHERE signing_secret IS NOT NULL").fetchone()[0] == 0
    assert h.conn.execute("SELECT final_message FROM interactions WHERE interaction_id = ?",
                          (iid,)).fetchone()[0] == registry.MSG_WITHDRAWN
    assert metrics.METRICS.membership_changes.value("self", "left") == 1
    # A POST from a session whose member has left is refused and creates nothing.
    for path in ("/account/webhook", "/account/webhook/remove", "/account/leave", "/account/logout"):
        assert (await h.post(path, second, {"automation_url": AUTOMATION_URL, "signing_secret": SECRET})).status == 403
    assert h.member() is None
    body = await (await h.page(second, "?done=left")).text()
    assert web_account.MSG_LEFT.replace("'", "&#x27;") in body and "Sign in with Discord" in body


async def test_leave_works_while_paused(h):
    session = await h.login()
    await h.save(session)
    pause(h.conn)
    assert (await h.post("/account/leave", session)).status == 303 and h.member() is None


async def test_sign_out_ends_only_that_session_and_works_while_paused(h):
    session = await h.login()
    other = await h.login()
    pause(h.conn)
    resp = await h.post("/account/logout", session)
    assert resp.status == 303 and resp.headers["Location"] == ORIGIN + "/account?done=signed_out"
    assert (await h.save(session)).status == 403
    assert sessions.load(h.conn, other, NOW) is not None and h.member() is not None


@pytest.mark.parametrize("path", ["leave_command", "osctl"])
async def test_leaving_by_any_path_deletes_the_sessions(h, conn, config, aiohttp_client, path):
    session = await h.login()
    if path == "leave_command":
        handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=lambda: NOW)
        await handler.handle(command("leave", confirm=True))
    else:
        admin = await aiohttp_client(create_admin_app(config, conn, lambda: None, clock=lambda: NOW))
        resp = await admin.delete(f"/v1/registrations/{CALLER}", headers={"Authorization": f"Bearer {ADMIN_TOKEN}"})
        assert resp.status == 200
    assert h.member() is None and h.count("web_sessions") == 0
    assert (await h.save(session)).status == 403 and h.member() is None


async def test_a_session_never_creates_a_member(h):
    """Joining happens only at login. A live session whose member is gone (ended here without the
    session cleanup, which no real path skips) is refused by every POST, and nothing is created."""
    session = await h.login()
    h.conn.execute("UPDATE registrations SET revoked_at = ? WHERE discord_user_id = ?", (NOW, CALLER))
    for path in ("/account/webhook", "/account/webhook/remove", "/account/leave"):
        resp = await h.post(path, session, {"automation_url": AUTOMATION_URL, "signing_secret": SECRET})
        assert resp.status == 403
    assert h.member() is None and h.count("registrations") == 2 and h.count("automations") == 0  # with the owner
    assert "Sign in with Discord" in await (await h.page(session)).text()


async def test_a_session_only_ever_acts_for_its_own_user(h):
    other_id = "666666666666666666"
    register(h.conn, other_id)
    session = await h.login()
    await h.post("/account/webhook", session, {"automation_url": OTHER_URL, "signing_secret": OTHER_SECRET,
                                               "discord_user_id": other_id})
    await h.post("/account/webhook/remove", session, {"discord_user_id": other_id})
    # The extra field is ignored: the save and the removal both hit the signed-in user's own automation.
    other = h.automation(other_id)
    assert other is not None and other["automation_url"] == automation_url_for(other_id)
    assert h.automation() is None
    assert metrics.METRICS.membership_changes.value("self", "webhook_set") == 1


# ------------------------------------------------------------------ another member's connector (ADR-0018)

def ban(conn, secret=SECRET):
    conn.execute("INSERT INTO banned_secrets (secret_hash, banned_at) VALUES (?, ?)", (foreign.secret_hash(secret), NOW))


async def test_a_banned_secret_is_refused_before_dns_and_not_counted(h):
    looked_up = []

    async def resolver(host):
        looked_up.append(host)
        return await public_resolver(host)

    h.http.app[http_app.ACCOUNT_KEY]._resolver = resolver
    session = await h.login()
    ban(h.conn)
    resp = await h.save(session)
    assert resp.status == 400 and html.escape(foreign.MSG_SECRET_BANNED, quote=False) in await resp.text()
    assert looked_up == [] and h.automation() is None and h.count("account_changes") == 0
    assert metrics.METRICS.membership_changes.value("self", "secret_banned") == 1


async def test_a_ban_that_lands_during_dns_is_refused_in_the_save_transaction(h, conn):
    session = await h.login()

    async def resolver(host):
        ban(conn)
        return await public_resolver(host)

    h.http.app[http_app.ACCOUNT_KEY]._resolver = resolver
    resp = await h.save(session)
    assert resp.status == 400 and html.escape(foreign.MSG_SECRET_BANNED, quote=False) in await resp.text()
    assert h.automation() is None and h.count("account_changes") == 0


async def test_a_url_another_member_holds_is_refused_with_a_new_automation_as_the_way_out(h, caplog):
    holder = register(h.conn, "666666666666666666", url=AUTOMATION_URL, secret=OTHER_SECRET)
    session = await h.login()
    with caplog.at_level("WARNING", logger="outer_steppes.web_account"):
        resp = await h.save(session)
    assert resp.status == 400 and html.escape(foreign.MSG_URL_IN_USE, quote=False) in await resp.text()
    assert h.automation() is None and h.count("account_changes") == 0
    me = h.member()["id"]
    assert [r.getMessage() for r in caplog.records] == [
        f"account page: trigger URL in use (holder {holder}, saver {me})"]
    assert AUTOMATION_URL not in caplog.text
    assert metrics.METRICS.membership_changes.value("self", "url_in_use") == 1


async def test_the_page_says_the_secret_is_blocked_and_warns_the_connectors_member(h):
    session = await h.login()
    assert (await h.save(session)).status == 303
    member = h.member()["id"]
    h.conn.execute("INSERT INTO oauth_tokens (token_hash, kind, client_id, registration_id, connection_id, "
                   "connected_at, expires_at, created_at) VALUES ('t', 'refresh', 'outer-steppes', ?, 'c', ?, ?, ?)",
                   (member, NOW - 10, NOW + 3600, NOW))
    ban(h.conn)
    h.conn.execute("UPDATE registrations SET foreign_use_at = ? WHERE id = ?", (NOW, member))
    page = html.unescape(await (await h.page(session)).text())
    blocked = foreign.text(foreign.MSG_BLOCKED_NOTE, h.config.membership).replace("**", "")
    assert blocked in page and foreign.MSG_CONNECTOR_USED.replace("**", "") in page and "**" not in page


# ------------------------------------------------------------------ sweeps and logs

async def test_sweep_removes_expired_sessions_and_old_save_counts(h, clock):
    session = await h.login()
    await h.save(session)
    removed = apply_retention(h.conn, NOW + sessions.SESSION_TTL_SECONDS)
    assert removed["web_sessions"] == 1 and removed["account_changes"] == 0
    removed = apply_retention(h.conn, NOW + sessions.CHANGE_WINDOW_SECONDS)
    assert removed["account_changes"] == 1


async def test_logs_never_contain_form_bodies_cookies_or_csrf_tokens(h, caplog):
    caplog.set_level(logging.DEBUG)
    session = await h.login()
    await h.save(session)
    await h.save(session, url="https://evil.example/x", secret=OTHER_SECRET)
    await h.save(session, secret="whsec_bad")
    await h.post("/account/logout", session)
    text = "\n".join(r.getMessage() for r in caplog.records)
    for secret in (session, sessions.csrf_for(session), SECRET, AUTOMATION_URL, OTHER_SECRET, "evil.example",
                   "whsec_bad"):
        assert secret not in text


async def test_a_save_renews_the_idle_clock_but_signing_in_and_a_refused_save_do_not(h):
    """PR 8d-1: the clock moves for a saved automation, not for signing in or a rejected form."""
    h.conn.execute("INSERT INTO registrations (id, discord_user_id, created_at, updated_at, last_used_at) "
                   "VALUES ('m', ?, 0, 0, 1)", (CALLER,))
    last_used = lambda: h.member()["last_used_at"]  # noqa: E731
    session = await h.login()
    assert last_used() == 1
    assert (await h.save(session, secret="not-a-secret")).status == 400
    assert last_used() == 1
    assert (await h.save(session)).status == 303
    assert last_used() == NOW


# ------------------------------------------------------------------ PR 8d-2: the connector's state and the leave text

def _token(h, expires, name="t"):
    h.conn.execute("INSERT INTO oauth_tokens (token_hash, kind, client_id, registration_id, connection_id, "
                   "connected_at, expires_at, created_at) VALUES (?, 'refresh', 'outer-steppes', ?, 'c', ?, ?, ?)",
                   (name, h.member()["id"], NOW, expires, NOW))


async def test_the_page_warns_about_the_connector_once_an_automation_is_set(h):
    """Answers arrive only through the connector (ADR-0017), so every member with an automation needs it."""
    session = await h.login()
    body = await (await h.page(session)).text()
    assert "connector is connected" not in body and "connector isn&#x27;t connected" not in body  # no automation

    await h.save(session)
    body = await (await h.page(session)).text()
    assert ("grok.com&#x27;s connector isn&#x27;t connected. Add or reconnect Outer Steppes in grok.com&#x27;s "
            "connector settings.") in body
    assert "**" not in body  # the page renders no markdown

    _token(h, NOW + 3600)
    body = await (await h.page(session)).text()
    # A live token ends the warning but earns no "connected" claim (DESIGN.md, Connector status)
    assert "isn&#x27;t connected" not in body and "connector is connected" not in body


async def test_viewing_the_page_does_not_renew_the_idle_clock(h):
    session = await h.login()
    h.conn.execute("UPDATE registrations SET last_used_at = 1")
    await h.page(session)
    assert h.conn.execute("SELECT last_used_at FROM registrations").fetchone()[0] == 1


def test_the_page_and_the_leave_command_say_the_same_thing():
    """DESIGN.md: the page shows the /leave text without its bold markers."""
    from outer_steppes import commands
    assert "**" in commands.MSG_LEFT and "**" not in web_account.MSG_LEFT
    assert web_account.MSG_LEFT == commands.MSG_LEFT.replace("**", "")
    assert "connector settings" in web_account.MSG_LEFT and "rotate its trigger secret" in web_account.MSG_LEFT


# ------------------------------------------------------------------ Grok Bot (ADR-0020)

BOT_KEY = "crsr_" + hashlib.sha256(b"made-up grok bot key").hexdigest()  # made up
BOT_URL = "https://api2.cursor.sh/automations/webhook/00000000-0000-4000-8000-0000000000b0"


async def test_the_account_page_saves_a_grok_bot_automation(h):
    session = await h.login()
    assert (await h.save(session, url=BOT_URL, secret=BOT_KEY)).status == 303
    automation = h.automation()
    assert (automation["kind"], automation["automation_url"]) == ("grok_bot_webhook", BOT_URL)


@pytest.mark.parametrize("url,secret", [(BOT_URL, SECRET), (AUTOMATION_URL, BOT_KEY)])
async def test_the_account_page_refuses_the_other_kinds_secret(h, url, secret):
    session = await h.login()
    resp = await h.save(session, url=url, secret=secret)
    assert resp.status == 400 and html.escape(web_account.MSG_SECRET_REJECTED, quote=False) in await resp.text()
    assert h.automation() is None
