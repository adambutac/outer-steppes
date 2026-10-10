"""The connector's status and the /ask refusal, on the real service (DESIGN.md, Member lifecycle)."""

from __future__ import annotations

from ..conftest import CALLER, command
from .conftest import eventually

DISCONNECTED_ASK = ("grok.com's connector isn't connected. Add or reconnect **Outer Steppes** in grok.com's "
                    "connector settings, then ask again.")
READY = "You're a member and your automation is set"


async def test_a_member_who_never_connected_is_refused_until_they_connect(boot):
    """Answers arrive only through the connector (ADR-0017), so every member needs it, including a new one."""
    h = await boot(answer="Hello.")
    await h.register()

    # No live connection: refused at ACK. Nothing is deferred, signed or sent, and it isn't counted.
    status, body = await h.interact(command())
    assert status == 200 and body["type"] == 4 and body["data"]["content"] == DISCONNECTED_ASK
    assert (await h.say(command("status"))).startswith(READY + ", but grok.com's connector isn't connected.")
    assert h.automation.posts == [] and h.discord.calls == []
    assert h.db().execute("SELECT count(*) FROM rate_events").fetchone()[0] == 0

    # The member connects: the same /ask goes through, and /status drops the warning.
    await h.connect()
    assert await h.say(command("status")) == READY + "."  # no "connected" claim
    status, ack = await h.interact(command())
    assert ack["type"] == 5
    await eventually(lambda: h.automation.replies, "the automation's discord_reply to return")
    assert h.discord.calls[0][2]["content"] == "> hello\nHello.\n\nWritten with Grok"


async def test_disconnecting_a_member_from_the_operator_side_makes_ask_refuse(boot):
    """The case the PR 8a pilot hit: `osctl disconnect-mcp` deletes the bridge's tokens, so the next /ask is refused
    at ACK. (Removing the connector inside grok.com is invisible to the bridge until its tokens expire: DESIGN.md,
    Connector status, "What it can't see".)"""
    h = await boot(answer="Hello.")
    await h.register()
    await h.connect()
    assert await h.say(command("status")) == READY + "."  # no warning, and no "connected" claim

    status, body = await h.operator("DELETE", f"/v1/registrations/{CALLER}/mcp-connections")
    assert status == 200 and body == {"disconnected": 1}

    assert (await h.say(command("status"))).startswith(READY + ", but grok.com's connector isn't connected.")
    status, answer = await h.interact(command())
    assert answer["type"] == 4 and answer["data"]["content"] == DISCONNECTED_ASK
    assert h.automation.posts == []
