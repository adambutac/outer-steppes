"""A member's onboarding through the real service: connect the connector, sign in to the account page, save a webhook,
get an answer through the MCP tool, and leave (DESIGN.md, Testing; the "Members and automations" and "The account page"
sections)."""

from __future__ import annotations

import json

from outer_steppes.oauth import MSG_CONNECTION_INVALID

from ..conftest import AUTOMATION_URL, SECRET, command
from .clients import GROK_CALLBACK, ORIGIN, Browser, Connector
from .conftest import eventually

USER = "555555555555555555"
INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}}


async def test_a_member_joins_saves_a_webhook_and_is_answered_through_the_connector(boot):
    h = await boot(login=True)

    # 1. Out of the box nobody joins: the bridge is personal and unclaimed (ADR-0016).
    stranger = await h.call("GET", "/account/login")
    refused = await h.call("GET", f"/oauth/discord/callback?state={stranger.query()['state']}&code="
                                  f"{h.discord.login_code(USER, 'member')}",
                           headers={"Cookie": f"__Host-os_login={stranger.cookie('__Host-os_login')}"})
    assert refused.status == 403 and "hasn&#x27;t been claimed yet" in refused.text
    assert await h.members() == []

    # 2. The owner claims it with the link from `osctl bridge claim`, then connects the connector in
    # grok.com, which ties it to their membership. There is no webhook yet.
    await h.claim(USER)
    assert await h.members() == [USER]
    connector = Connector(h)
    tokens = await connector.sign_in(USER)
    assert await h.members() == [USER]
    assert (await h.say(command("status", caller=USER))).startswith("You're a member, but you haven't set an automation")
    assert await h.say(command(caller=USER)) == "You haven't set an automation yet. Run /webhook set."
    assert h.discord.token_requests[0]["client_secret"] == "made-up-discord-client-secret"  # the bridge's own secret

    # 3. They sign in to the account page with Discord and save the webhook.
    browser = Browser(h)
    await browser.sign_in(USER)
    page = await browser.page()
    assert page.status == 200 and "Signed in as" in page.text and "Not set." in page.text
    saved = await browser.post("/account/webhook", {"automation_url": AUTOMATION_URL, "signing_secret": SECRET})
    assert (saved.status, saved.location) == (303, ORIGIN + "/account?done=saved")
    page = await browser.page(query="?done=saved")
    assert "Saved. Your automation is set." in page.text
    assert AUTOMATION_URL not in page.text and SECRET not in page.text  # never shown back

    # 4. /ask runs the automation, which fetches the prompt and answers through the connector (ADR-0017).
    status, ack = await h.interact(command(caller=USER, prompt="what is 2 + 2?"))
    assert status == 200 and ack["type"] == 5
    [payload] = await eventually(lambda: h.automation.posts[:1] or None, "the automation to be called")

    assert (await connector.mcp(tokens.access, "initialize", INIT)).json()["result"]["serverInfo"]["name"] == "outer-steppes"
    listed = (await connector.mcp(tokens.access, "tools/list")).json()["result"]["tools"]
    assert [t["name"] for t in listed] == ["get_prompt", "discord_reply"]
    fetched = await connector.mcp(tokens.access, "tools/call", {
        "name": "get_prompt", "arguments": {"reply_token": payload["reply_token"]}})
    assert json.loads(fetched.json()["result"]["content"][0]["text"])["prompt"] == "what is 2 + 2?"
    called = await connector.mcp(tokens.access, "tools/call", {
        "name": "discord_reply", "arguments": {"reply_token": payload["reply_token"], "content": "four"}})
    assert called.json()["result"] == {"content": [{"type": "text", "text": "Posted to Discord."}], "isError": False}

    [(kind, _, body)] = h.discord.calls
    assert kind == "edit" and body["content"] == "> what is 2 + 2?\nfour\n\nWritten with Grok"
    assert await h.say(command("status", caller=USER)) == "You're a member and your automation is set."
    assert h.automation.rejected == []


async def test_the_session_cookie_and_the_form_protections_hold_on_the_real_listener(boot):
    h = await boot(login=True, claimed_by=USER)
    await Connector(h).sign_in(USER)  # a member exists
    browser = Browser(h)
    await browser.sign_in(USER)

    cookie = browser.session_set_cookie
    assert cookie.startswith("__Host-os_session=")
    for attribute in ("Secure", "HttpOnly", "SameSite=Lax", "Path=/"):
        assert attribute in cookie, cookie
    assert "Domain" not in cookie and "Max-Age=1800" in cookie  # no Domain (a __Host- cookie), and it lasts 30 minutes

    form = {"automation_url": AUTOMATION_URL, "signing_secret": SECRET}
    real_csrf = await browser.csrf()  # a token that is right for this session, so only the cookie is missing below
    refused = {
        "no session cookie": await browser.post("/account/webhook", form, with_session=False, csrf=real_csrf),
        "wrong csrf": await browser.post("/account/webhook", form, csrf="0" * 64),
        "foreign origin": await browser.post("/account/webhook", form, origin="https://evil.example"),
        "no origin": await browser.post("/account/webhook", form, origin=None),
    }
    for why, reply in refused.items():
        assert reply.status == 403, (why, reply.status)
    assert h.db().execute("SELECT count(*) FROM automations").fetchone()[0] == 0  # nothing was written

    assert (await h.call("HEAD", "/account/login")).status == 405  # a link preview can't start a login
    anonymous = await h.call("GET", "/account")
    assert anonymous.status == 200 and "Sign in with Discord" in anonymous.text and "name=csrf" not in anonymous.text


async def test_removing_the_webhook_keeps_the_member_and_the_connector(boot):
    h = await boot(login=True, claimed_by=USER)
    tokens = await Connector(h).sign_in(USER)
    browser = Browser(h)
    await browser.sign_in(USER)
    await browser.post("/account/webhook", {"automation_url": AUTOMATION_URL, "signing_secret": SECRET})

    removed = await browser.post("/account/webhook/remove")
    assert (removed.status, removed.location) == (303, ORIGIN + "/account?done=removed")
    assert await h.members() == [USER]  # still a member
    assert (await Connector(h).mcp(tokens.access, "tools/list")).status == 200  # and the connector stays connected
    assert await h.say(command(caller=USER)) == "You haven't set an automation yet. Run /webhook set."


async def test_leaving_from_the_page_ends_the_session_and_the_connector_is_told_to_reconnect(boot):
    h = await boot(login=True, claimed_by=USER)
    connector = Connector(h)
    tokens = await connector.sign_in(USER)
    browser = Browser(h)
    await browser.sign_in(USER)
    await browser.post("/account/webhook", {"automation_url": AUTOMATION_URL, "signing_secret": SECRET})

    csrf = await browser.csrf()  # what the page gave this session, kept to try after leaving
    left = await browser.post("/account/leave")
    assert (left.status, left.location) == (303, ORIGIN + "/account?done=left")
    assert await h.members() == []
    notice = await browser.page(query="?done=left")
    assert "You&#x27;ve left Outer Steppes." in notice.text and "Sign in with Discord" in notice.text
    # A session never outlives the membership: its row is deleted, and the old cookie can no longer do anything.
    # (The page alone can't show this, because a session with no member renders the sign-in view too.)
    assert h.db().execute("SELECT count(*) FROM web_sessions").fetchone()[0] == 0
    reused = await browser.post("/account/webhook", {"automation_url": AUTOMATION_URL, "signing_secret": SECRET},
                                csrf=csrf)
    assert reused.status == 403 and await h.members() == []  # refused, and it created no member

    # The connector's calls are refused, and told what to do.
    dead = await connector.mcp(tokens.access, "tools/list")
    assert dead.status == 401
    assert dead.headers["WWW-Authenticate"] == (
        f'Bearer realm="outer-steppes", error="invalid_token", error_description="{MSG_CONNECTION_INVALID}"')
    refreshed = await connector.refresh(tokens.refresh)
    assert (refreshed.status, refreshed.json()) == (
        400, {"error": "invalid_grant", "error_description": MSG_CONNECTION_INVALID})
    assert h.db().execute("SELECT count(*) FROM oauth_tokens").fetchone()[0] == 0
    row = h.db().execute("SELECT count(*) FROM automations WHERE automation_url IS NOT NULL").fetchone()[0]
    assert row == 0  # the webhook's URL and secret are wiped


async def test_the_code_exchange_needs_the_right_verifier_and_redirect_and_works_once(boot):
    h = await boot(login=True, claimed_by=USER)
    connector = Connector(h)
    good = {"grant_type": "authorization_code", "redirect_uri": GROK_CALLBACK, "code_verifier": connector.verifier,
            "resource": connector.resource}
    wrong_verifier = {**good, "code_verifier": "v" * 64}
    wrong_redirect = {**good, "redirect_uri": "https://evil.example/callback"}
    for why, form in (("verifier", wrong_verifier), ("redirect", wrong_redirect)):
        code = await connector.code_for(USER)  # a failed attempt uses the code up, so each case gets its own
        refused = await connector.token({**form, "code": code})
        assert (refused.status, refused.json()) == (400, {"error": "invalid_grant"}), why
        assert (await connector.token({**good, "code": code})).status == 400, why  # and it is gone now
    code = await connector.code_for(USER)
    assert (await connector.token({**good, "code": code})).status == 200
    assert (await connector.token({**good, "code": code})).status == 400  # single use


async def test_the_page_refuses_a_webhook_it_cant_accept_and_never_echoes_it(boot):
    h = await boot(login=True, claimed_by=USER)
    await Connector(h).sign_in(USER)
    browser = Browser(h)
    await browser.sign_in(USER)

    elsewhere = "https://evil.example/webhook/automation/00000000-0000-4000-8000-000000000001"
    bad_url = await browser.post("/account/webhook", {"automation_url": elsewhere, "signing_secret": SECRET})
    bad_secret = await browser.post("/account/webhook", {"automation_url": AUTOMATION_URL, "signing_secret": "whsec_x"})
    assert (bad_url.status, bad_secret.status) == (400, 400)
    assert elsewhere not in bad_url.text and "evil.example" not in bad_url.text
    assert "whsec_x" not in bad_secret.text and AUTOMATION_URL not in bad_secret.text  # no value is echoed back
    assert h.db().execute("SELECT count(*) FROM automations").fetchone()[0] == 0
