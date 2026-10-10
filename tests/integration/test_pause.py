"""The kill switch through the real service: `osctl bridge pause` and `unpause` against the admin listener
(DESIGN.md, Registration (operator): Pause, and ACK steps 8 and 9; ADR-0004)."""

from __future__ import annotations

import asyncio
import importlib.util
import io
import time
from pathlib import Path

from ..conftest import ADMIN_TOKEN, AUTOMATION_URL, CALLER, command
from .conftest import eventually

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "osctl.py"
spec = importlib.util.spec_from_file_location("osctl_for_integration", SCRIPT)
osctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(osctl)

OTHER = "666666666666666666"
OTHER_URL = AUTOMATION_URL[:-1] + "2"
WITHDRAWN = "Grok is paused. This request was withdrawn."
PAUSED = "Grok is paused."


async def osctl_says(h, *argv: str) -> tuple[int, str]:
    """Run the real `osctl bridge …` against the admin listener, as it runs where OSCTL_ADMIN_URL is set. It is
    synchronous (urllib), so it runs in a thread while the service keeps serving on this loop. Its poll after a
    pause waits 0.05 s between tries instead of 1 s, so the real loop runs without a fixed second's sleep."""
    out = io.StringIO()
    code = await asyncio.to_thread(osctl.run, list(argv), osctl.AdminClient(h.admin, ADMIN_TOKEN), out=out,
                                   sleep=lambda _: time.sleep(0.05))
    return code, out.getvalue()


def edits_saying(h, text: str) -> list[str]:
    """The interaction tokens whose deferred message Discord was told to edit to `text`. Only edits: a withdrawal
    sent as a followup would leave "thinking…" in place, so it doesn't count."""
    return [token for kind, token, body in h.discord.calls if kind == "edit" and body.get("content") == text]


async def test_pause_withdraws_queued_and_posted_work_refuses_new_work_and_unpause_restores_it(boot):
    # One slot for sends, so a second ask waits for it while the first one's POST is in flight.
    h = await boot(max_concurrent_automation_posts=1)
    await h.register()
    await h.connect()

    # The first ask is posted: its reply token is minted and committed, and its POST is held on the way out.
    h.poster.gate.clear()
    await h.interact({**command(prompt="first"), "token": "token-posted"})
    await eventually(lambda: h.poster.held == 1 or None, "the first ask's POST to be in flight")
    # The second is queued: its worker waits for the slot, so it is never signed while it waits.
    await h.interact({**command(prompt="second"), "token": "token-queued"})
    statuses = {r["interaction_token"]: r["status"] for r in h.db().execute(
        "SELECT interaction_token, status FROM interactions WHERE interaction_token IS NOT NULL")}
    assert statuses == {"token-posted": "posted", "token-queued": "queued"}
    queued_corr = h.db().execute("SELECT correlation_id FROM interactions WHERE interaction_token = 'token-queued'"
                                 ).fetchone()[0]

    # The operator pauses. osctl reports both withdrawn and waits until Discord has been told.
    code, said = await osctl_says(h, "bridge", "pause")
    assert code == 0, said
    assert "paused; 2 pending request(s) withdrawn" in said
    assert "all withdrawal messages sent; safe to stop the process" in said
    assert sorted(edits_saying(h, WITHDRAWN)) == ["token-posted", "token-queued"]
    code, said = await osctl_says(h, "bridge", "status")
    assert (code, said.strip()) == (0, "paused: True; pending final messages: 0")
    await eventually(lambda: len(h.stack.held) == 0 or None, "the withdrawn asks' prompts to be dropped")

    # The POST already committed still goes out (DESIGN.md, What "withdrawn" means), and its worker frees the slot
    # for the queued ask's, which finds its row withdrawn and stops.
    h.poster.gate.set()
    [posted] = await eventually(lambda: h.automation.posts[:1] or None, "the first ask's POST to arrive")

    # The posted ask's run comes back: its token is closed, so it reads nothing and posts nothing.
    fetched = await h.automation.get_prompt(posted["reply_token"])
    replied = await h.automation.send_reply(posted["reply_token"], "too late")
    assert fetched["isError"] and h.automation.text(fetched).startswith("closed:")
    assert replied["isError"] and h.automation.text(replied).startswith("closed:")
    assert "first" not in str(fetched)

    # While paused, /ask is refused before anything is deferred, and only the read-only commands answer.
    status, refused = await h.interact(command(prompt="third"))
    assert (status, refused["type"], refused["data"]["content"]) == (200, 4, PAUSED)
    assert await h.say(command("status")) == PAUSED + " You're a member and your automation is set."
    assert (await h.say(command("webhook", sub="status"))).startswith(PAUSED + " Your automation is set (since <t:")
    assert (await h.say(command("settings"))).startswith("Your answers are private by default")
    assert await h.say(command("webhook", sub="set")) == PAUSED

    # Unpausing restores service: a new /ask is posted and answered.
    code, said = await osctl_says(h, "bridge", "unpause")
    assert (code, said.strip()) == (0, "unpaused")
    h.automation.answer = "Back again."
    status, ack = await h.interact({**command(prompt="fourth"), "token": "token-after"})
    assert (status, ack["type"]) == (200, 5)
    await eventually(lambda: h.automation.replies, "the answer after unpausing to be delivered")
    assert [b["content"] for _, t, b in h.discord.calls if t == "token-after"] == ["> fourth\nBack again.\n\nWritten with Grok"]

    # Only the first ask and the one after unpausing were ever sent to the automation. The queued one never was:
    # with one slot, its worker had to finish before the later ask's could send.
    assert len(h.automation.posts) == 2
    assert queued_corr not in [p["correlation_id"] for p in h.automation.posts]
    assert h.automation.rejected == []
    row = h.db().execute("SELECT status, posted_at FROM interactions WHERE correlation_id = ?",
                         (queued_corr,)).fetchone()
    assert (row["status"], row["posted_at"]) == ("failed", None)


async def test_leaving_and_removing_the_automation_still_work_while_paused(boot):
    """The kill switch never keeps a member's secret or tokens on the bridge (DESIGN.md, ACK step 8)."""
    h = await boot()
    await h.register()
    await h.register(OTHER, url=OTHER_URL)
    await h.connect()
    code, said = await osctl_says(h, "bridge", "pause")
    assert code == 0 and "paused; 0 pending request(s) withdrawn" in said

    assert await h.say(command("webhook", sub="remove")) == (
        "Automation removed. You're still a member, and this doesn't disconnect grok.com's connector.")
    assert (await h.say(command("leave", confirm=True, caller=OTHER))).startswith("You've left Outer Steppes.")

    assert await h.members() == [CALLER]
    rows = h.db().execute("SELECT count(*) FROM automations WHERE automation_url IS NOT NULL").fetchone()[0]
    assert rows == 0  # both URLs and secrets are wiped
    assert await h.say(command("status")) == (
        PAUSED + " You're a member, but you haven't set an automation yet. Run /webhook set.")
