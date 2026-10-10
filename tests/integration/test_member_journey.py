"""A member's journey through the real service: /ask, the signed POST, the prompt fetch, the answer, and leaving."""

from __future__ import annotations

import json

from ..conftest import CALLER, CHANNEL, command
from .conftest import eventually

OTHER = "666666666666666666"


async def test_a_member_asks_the_automation_answers_and_discord_shows_it(boot):
    h = await boot(answer="It is four.")
    await h.register()
    await h.connect()

    status, ack = await h.interact(command(prompt="what is 2 + 2?"))
    assert status == 200 and ack["type"] == 5  # deferred; private by default, so flagged ephemeral
    assert ack["data"] == {"flags": 64}

    await eventually(lambda: h.discord.calls, "the answer to reach Discord")
    kind, token, body = h.discord.calls[0]
    assert (kind, token) == ("edit", "interaction-token-abc")
    assert body == {"content": "> what is 2 + 2?\nIt is four.\n\nWritten with Grok", "allowed_mentions": {"parse": []}}

    # The automation received exactly the payload DESIGN.md describes: no prompt and no Discord ids. The bridge's
    # signature verified with the stored secret (a bad signature would be a 401 in `rejected`).
    assert h.automation.rejected == []
    [payload] = h.automation.posts
    assert list(payload) == ["instructions", "correlation_id", "reply_token", "reply_expires_at"]
    assert payload["reply_token"].startswith("rt_")

    # The run fetched the prompt, caller and channel through the member's connector.
    [fetched] = h.automation.fetches
    assert not fetched["isError"]
    assert json.loads(h.automation.text(fetched)) == {"prompt": "what is 2 + 2?", "discord_user_id": CALLER,
                                                      "channel_id": CHANNEL}

    # The automation's discord_reply returns after the bridge has delivered to Discord and marked the row
    # answered. Discord saw the edit first, so wait for that call to finish before asserting on either.
    await eventually(lambda: h.automation.replies, "the automation's discord_reply to return")
    assert [h.automation.text(r) for r in h.automation.replies] == ["Posted to Discord."]

    row = h.db().execute("SELECT status, interaction_token FROM interactions WHERE status != 'done'").fetchone()
    assert (row["status"], row["interaction_token"]) == ("answered", None)  # the Discord token is dropped after use
    columns = [c[1] for c in h.db().execute("PRAGMA table_info(interactions)")]
    assert "prompt_mac" in columns and not any("excerpt" in c for c in columns)  # no prompt text is stored


async def test_a_reply_token_works_once(boot):
    h = await boot()
    await h.register()
    await h.connect()
    await h.interact(command())
    [payload] = await eventually(lambda: h.automation.posts[:1] or None, "the automation to be called")
    token = payload["reply_token"]
    first = await h.automation.send_reply(token, "first")
    second = await h.automation.send_reply(token, "second")
    assert (h.automation.text(first), second["isError"]) == ("Posted to Discord.", True)
    assert h.automation.text(second).startswith("used:")
    assert [c[2]["content"] for c in h.discord.calls] == ["> hello\nfirst\n\nWritten with Grok"]


async def test_a_trigger_from_an_account_without_the_members_connector_gets_nothing(boot):
    """ADR-0017: the member saved a trigger whose grok.com account doesn't hold their connector. Its run gets an
    opaque token, and neither the token alone nor another member's connector reads the prompt or posts. Another
    member's connector ends the ask and bans the member's secret instead (ADR-0018)."""
    h = await boot()
    await h.register()
    await h.connect()  # the member's own connector exists, but elsewhere
    await h.register(OTHER, url="https://grok.com/webhook/automation/00000000-0000-4000-8000-000000000002")
    other_token = await h.connect(OTHER)
    await h.interact(command(prompt="something private"))
    [payload] = await eventually(lambda: h.automation.posts[:1] or None, "the automation to be called")

    h.automation.access_token = None  # no connector at all: the bridge never sees the call
    assert (await h.automation.get_prompt(payload["reply_token"]))["status"] == 401
    assert (await h.automation.send_reply(payload["reply_token"], "x"))["status"] == 401

    assert h.db().execute("SELECT status FROM interactions WHERE status != 'done'").fetchone()[0] == "posted"
    assert h.discord.calls == []

    h.automation.access_token = other_token  # a connector bound to someone else
    fetched = await h.automation.get_prompt(payload["reply_token"])
    replied = await h.automation.send_reply(payload["reply_token"], "x")
    assert fetched["isError"] and h.automation.text(fetched).startswith("unknown:")
    assert replied["isError"] and h.automation.text(replied).startswith("unknown:")
    assert "something private" not in json.dumps([fetched, replied])
    # The ask ended at once, and Discord says why instead of "thinking..." (T11b).
    [ended] = await eventually(lambda: [c for c in h.discord.calls
                                        if c[2]["content"].startswith("Grok's run used an **Outer Steppes** connector")],
                               "Discord to be told the request was stopped")
    assert h.db().execute("SELECT status FROM interactions WHERE status != 'done'").fetchone()[0] == "failed"
    assert h.db().execute("SELECT count(*) FROM banned_secrets").fetchone()[0] == 1
    assert len(h.stack.held) == 0  # the finalizer dropped the ended ask's prompt
    assert (await h.say(command(prompt="again"))).startswith("Your automation's signing secret is blocked")


async def test_leaving_ends_the_membership_and_a_late_answer_is_refused(boot):
    h = await boot()
    await h.register()
    await h.connect()
    await h.interact(command())
    [payload] = await eventually(lambda: h.automation.posts[:1] or None, "the automation to be called")
    assert await h.members() == [CALLER]

    assert (await h.say(command("leave", confirm=True))).startswith("You've left Outer Steppes.")
    assert await h.members() == []
    assert (await h.say(command("status"))).startswith("You're not a member yet")

    # The member's pending request is withdrawn, and Discord shows that instead of "thinking...".
    await eventually(lambda: [c for c in h.discord.calls if c[2]["content"] == "This reply was withdrawn."],
                     "Discord to be told the reply was withdrawn")

    # The finalizer pass that sent it also dropped the held prompt (DESIGN.md, ACK step 13).
    await eventually(lambda: len(h.stack.held) == 0 or None, "the withdrawn ask's prompt to be dropped")

    # The automation answers after the member left: leaving ended the connector too, and nothing more reaches
    # Discord.
    late = await h.automation.send_reply(payload["reply_token"], "too late")
    assert late["status"] == 401
    row = h.db().execute("SELECT automation_url, signing_secret FROM automations").fetchone()
    assert (row["automation_url"], row["signing_secret"]) == (None, None)  # the secret is gone, not just unused
    assert [c for c in h.discord.calls if "too late" in c[2]["content"]] == []
