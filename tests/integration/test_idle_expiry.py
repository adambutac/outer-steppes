"""Idle expiry on a database file that already holds members, through the real service and its real sweeper."""

from __future__ import annotations

import time

from outer_steppes import db

from ..conftest import command
from .conftest import eventually

DAY = 86_400
OLD_USER, RECENT_USER = "10", "11"


def two_members(path: str) -> None:
    """Rows written before the service boots: two members with webhooks, one last used 200 days ago."""
    db.initialize(path)
    conn = db.connect(path, 100)
    now = int(time.time())
    for member_id, user, used in (("old", OLD_USER, now - 200 * DAY), ("recent", RECENT_USER, now - DAY)):
        conn.execute("INSERT INTO registrations (id, discord_user_id, last_used_at, created_at, updated_at) "
                     "VALUES (?, ?, ?, ?, ?)", (member_id, user, used, used, used))
        conn.execute("INSERT INTO automations (id, registration_id, kind, automation_url, signing_secret, "
                     "created_at) VALUES (?, ?, 'grok_webhook', 'https://grok.com/webhook/automation/' || ?, "
                     "'whsec_x', ?)", (member_id, member_id, member_id, used))
    # An already-expired login session: the retention pass deletes it, which is how a test can tell it ran.
    conn.execute("INSERT INTO web_sessions (session_hash, discord_user_id, username, expires_at) "
                 "VALUES ('stale', ?, 'n', ?)", (RECENT_USER, now - 10))
    conn.close()


async def test_an_idle_member_is_expired_by_the_running_sweeper(boot):
    h = await boot(sweep_interval=0.05, before_start=two_members)  # MEMBER_IDLE_DAYS stays at its default, 180

    await eventually(_only(h, RECENT_USER), "the idle member to be expired by the sweeper")

    # Read back through what a member and the operator use.
    assert (await h.say(command("status", caller=OLD_USER))).startswith("You're not a member yet")
    # Still a member (with no connector, so the status adds the connector warning).
    assert (await h.say(command("status", caller=RECENT_USER))).startswith("You're a member and your automation is set")
    async with h.http.get(h.admin + "/metrics") as resp:
        assert 'bridge_membership_changes_total{source="self",result="idle_left"} 1' in await resp.text()
    gone = h.db().execute("SELECT automation_url, signing_secret FROM automations WHERE id = 'old'").fetchone()
    assert (gone["automation_url"], gone["signing_secret"]) == (None, None)


async def test_with_expiry_off_nobody_is_expired(boot):
    h = await boot(member_idle_days=0, sweep_interval=0.05, before_start=two_members)
    # Wait for evidence that the retention pass ran (it deletes the stale session), then check the members.
    await eventually(lambda: h.db().execute("SELECT count(*) FROM web_sessions").fetchone()[0] == 0,
                     "the retention pass to run")
    assert sorted(await h.members()) == [OLD_USER, RECENT_USER]


def _only(h, user):
    async def check():
        return await h.members() == [user]
    return check
