"""USER_ALLOWLIST through the real service: a listed-out user is refused everywhere except leaving
(DESIGN.md, Install contexts; ACK steps 8 and 9; Callback outcomes)."""

from __future__ import annotations

from ..conftest import AUTOMATION_URL, CALLER, command
from .clients import Browser, Connector

LISTED = "666666666666666666"
LISTED_URL = AUTOMATION_URL[:-1] + "2"
NOT_ALLOWED = "Not allowed here."


def bot_dm(name: str = "ask", **options) -> dict:
    """The command in a DM with the bot, through a user install: no server, so no GUILD_ALLOWLIST or
    CHANNEL_ALLOWLIST applies, only USER_ALLOWLIST (DESIGN.md, Install contexts)."""
    body = command(name, **options)
    del body["guild_id"], body["member"]
    return {**body, "context": 1, "user": {"id": CALLER}, "authorizing_integration_owners": {"1": CALLER},
            "channel": {"id": body["channel_id"], "type": 1}}


async def test_a_member_outside_user_allowlist_is_refused_everywhere_but_can_still_leave(boot):
    h = await boot(user_allowlist=frozenset({LISTED}), answer="Hello.")
    # The operator adds members whatever the allowlist says (DESIGN.md, Members and automations: how a member is
    # created). CALLER, the member under test, isn't listed (say, listed once and taken off since); LISTED is.
    await h.register()
    await h.register(LISTED, url=LISTED_URL)
    await h.connect()

    refused = {
        "/ask in a server": command(prompt="hello"),
        "/ask in a bot DM": bot_dm(prompt="hello"),
        "/status": command("status"),
        "/status in a bot DM": bot_dm("status"),
        "/settings": command("settings"),
        "/settings private:false": command("settings", private=False),
        "/webhook status": command("webhook", sub="status"),
        "/webhook set": command("webhook", sub="set"),
        "an unknown command": command("unknown"),
    }
    for what, interaction in refused.items():
        status, body = await h.interact(interaction)
        assert (status, body["type"], body["data"]["content"]) == (200, 4, NOT_ALLOWED), what

    # Nothing was started, counted or saved for them.
    assert h.automation.posts == [] and h.discord.calls == []
    assert h.db().execute("SELECT count(*) FROM rate_events").fetchone()[0] == 0
    assert h.db().execute("SELECT count(*) FROM member_settings").fetchone()[0] == 0

    # A listed member, in the same server and channel, is served as usual.
    assert (await h.say(command("status", caller=LISTED))).startswith("You're a member and your automation is set")

    # Leaving is never blocked (ACK step 8): /leave's warning, removing the automation, then leaving all answer.
    assert (await h.say(command("leave"))).startswith("Leaving Outer Steppes deletes your automation")
    assert await h.say(command("webhook", sub="remove")) == (
        "Automation removed. You're still a member, and this doesn't disconnect grok.com's connector.")
    assert (await h.say(command("leave", confirm=True))).startswith("You've left Outer Steppes.")
    assert await h.members() == [LISTED]
    wiped = h.db().execute("SELECT automation_url FROM automations WHERE automation_url IS NOT NULL").fetchall()
    assert [r[0] for r in wiped] == [LISTED_URL]


async def test_a_user_outside_user_allowlist_cant_connect_or_sign_in(boot):
    """The Discord login applies USER_ALLOWLIST before anything else that could admit them (Callback outcomes)."""
    h = await boot(login=True, membership="shared", claimed_by=LISTED, user_allowlist=frozenset({LISTED}))

    connector = Connector(h)
    started = await connector.authorize()
    nonce = started.cookie("__Host-os_login")
    refused = await h.call("GET", f"/oauth/discord/callback?state={started.link_query('Continue')['state'][0]}"
                                  f"&code={h.discord.login_code(CALLER, 'outsider')}",
                           headers={"Cookie": f"__Host-os_login={nonce}"})
    assert refused.status == 403 and refused.location is None
    assert "Signed in to Discord as outsider, but this bridge can&#x27;t add you: that account isn&#x27;t allowed " \
           "here." in refused.text

    page = await h.call("GET", "/account/login")
    refused = await h.call("GET", f"/oauth/discord/callback?state={page.query()['state']}"
                                  f"&code={h.discord.login_code(CALLER, 'outsider')}",
                           headers={"Cookie": f"__Host-os_login={page.cookie('__Host-os_login')}"})
    assert refused.status == 403 and "that account isn&#x27;t allowed here." in refused.text
    assert refused.cookie("__Host-os_session") is None

    assert await h.members() == [LISTED]  # a shared bridge with room, and still nobody was added
    # The listed owner connects and signs in as usual.
    assert (await Connector(h).sign_in(LISTED)).access
    assert (await Browser(h).sign_in(LISTED)).status == 303


async def test_a_user_outside_user_allowlist_cant_claim_the_bridge_with_its_link(boot):
    """The claim link would make its opener the owner of an unclaimed bridge, so USER_ALLOWLIST applies to it too
    (DESIGN.md, Claiming the bridge)."""
    h = await boot(login=True, user_allowlist=frozenset({LISTED}))

    status, minted = await h.operator("POST", "/v1/claim")
    assert status == 200, minted
    started = await h.call("GET", minted["claim_url"][len(h.config.public_base_url):])
    assert started.status == 302, started.text
    refused = await h.call("GET", f"/oauth/discord/callback?state={started.query()['state']}"
                                  f"&code={h.discord.login_code(CALLER, 'outsider')}",
                           headers={"Cookie": f"__Host-os_login={started.cookie('__Host-os_login')}"})
    assert refused.status == 403 and "that account isn&#x27;t allowed here." in refused.text
    assert refused.cookie("__Host-os_session") is None
    assert await h.members() == []  # still unclaimed

    # A listed user claims it with a new link as usual.
    await h.claim(LISTED)
    assert await h.members() == [LISTED]
