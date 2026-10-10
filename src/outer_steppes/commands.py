"""Discord command handlers that run inside the ACK transaction.

/ask admission lives in interactions.py; these are the other commands.
Each returns the ephemeral reply text and whether the finalizer must be woken
after the response is written (DESIGN.md, ACK steps 8 and 10).
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from urllib.parse import urlsplit

from . import foreign, registry
from .config import OPERATOR
from .policy import MSG_PAUSED, is_paused

# DESIGN.md, Member lifecycle > What leaving does to grok.com.
MSG_LEFT = registry.MSG_LEFT  # shared with the account page (DESIGN.md, Member lifecycle)
MSG_LEAVE_EXPLAINED = ("Leaving Outer Steppes deletes your automation and every grok.com connector connection, "
                       "and ends your membership. Your /settings are kept. To leave, run /leave confirm:True. "
                       "To remove only your automation, run /webhook remove.")
MSG_AUTOMATION_REMOVED = "Automation removed. You're still a member, and this doesn't disconnect grok.com's connector."
MSG_NO_AUTOMATION_TO_REMOVE = "You have no automation set. Nothing to remove."
MSG_WEBHOOK_OPERATOR_ONLY = "Ask the operator to set your automation."
MSG_WEBHOOK_LINK = ("Set or replace your automation on the account page: {url}\n"
                    "Check that the address starts with {host} before pasting a secret. "
                    "Never paste a webhook URL or secret in Discord.")
MSG_MEMBER_READY = "You're a member and your automation is set."
MSG_MEMBER_NO_AUTOMATION = "You're a member, but you haven't set an automation yet. Run /webhook set."


@dataclass(frozen=True)
class CommandResult:
    message: str
    wake_finalizer: bool = False
    # A bridge_membership_changes_total result (source "self"), counted only after the ACK commits.
    membership_change: str | None = None


def not_a_member(membership: str) -> CommandResult:
    return CommandResult(registry.not_member_message(membership))


def leave(conn: sqlite3.Connection, caller_id: str, confirm: object, now: int, membership: str) -> CommandResult:
    """End the membership, but only for `confirm:True`; a bare /leave just explains (never acts)."""
    registration = registry.active_registration(conn, caller_id)
    if registration is None:
        return not_a_member(membership)
    if confirm is not True:
        return CommandResult(MSG_LEAVE_EXPLAINED)
    withdrawn = registry.revoke_in_tx(conn, registration["id"], now)
    return CommandResult(MSG_LEFT, wake_finalizer=withdrawn > 0, membership_change="left")


def webhook_remove(conn: sqlite3.Connection, caller_id: str, now: int, membership: str) -> CommandResult:
    """Remove only the automation, as DELETE …/automation does. Idempotent."""
    registration = registry.active_registration(conn, caller_id)
    if registration is None:
        return not_a_member(membership)
    removed, withdrawn = registry.remove_automation_in_tx(conn, registration["id"], now)
    return CommandResult(MSG_AUTOMATION_REMOVED if removed else MSG_NO_AUTOMATION_TO_REMOVE,
                         wake_finalizer=withdrawn > 0, membership_change="webhook_removed" if removed else None)


def webhook_set(public_base_url: str, membership: str) -> CommandResult:
    """Only a link to the account page: no secret ever goes through Discord.

    A non-member gets the link too, because on a shared bridge signing in there creates the member.
    """
    if membership == OPERATOR:
        return CommandResult(MSG_WEBHOOK_OPERATOR_ONLY)
    host = urlsplit(public_base_url).netloc
    return CommandResult(MSG_WEBHOOK_LINK.format(url=public_base_url + "/account", host=host))


def webhook_status(conn: sqlite3.Connection, caller_id: str, membership: str, now: int) -> CommandResult:
    """Whether an automation is set and when, and the default visibility. Never the URL or secret."""
    registration = registry.active_registration(conn, caller_id)
    if registration is None:
        return not_a_member(membership)
    automation = registry.active_automation(conn, registration["id"])
    if automation is None:
        text = "You haven't set an automation yet. Run /webhook set."
    else:
        text = f"Your automation is set (since <t:{automation['created_at']}:f>)."
    visibility = "private" if answers_private(conn, caller_id) else "public"
    text += f" Your answers are {visibility} by default."
    # Only the warning, and only with an automation, whose answers need the connector; no "connected" claim
    # (DESIGN.md, Connector status)
    if automation is not None and registry.connector_state(conn, registration, now) == "disconnected":
        text += " " + registry.MSG_CONNECTOR_DOWN + "."
    elif automation is not None and foreign.automation_banned(conn, automation["id"]):
        text += " " + foreign.text(foreign.MSG_BLOCKED_NOTE, membership)
    # The connector's member's warning shows whatever else is true (DESIGN.md, Connector status).
    if foreign.warned(conn, registration["id"], now):
        text += " " + foreign.MSG_CONNECTOR_USED
    if is_paused(conn):
        text = f"{MSG_PAUSED} {text}"
    return CommandResult(text)


def answers_private(conn: sqlite3.Connection, caller_id: str) -> bool:
    """The member's default visibility for /ask: private unless they chose public."""
    row = conn.execute(
        "SELECT answers_private FROM member_settings WHERE discord_user_id = ?", (caller_id,)
    ).fetchone()
    return True if row is None else bool(row["answers_private"])


def settings(conn: sqlite3.Connection, caller_id: str, private: object, now: int) -> CommandResult:
    """Show or set the member's default. Only members have settings; an automation isn't needed."""
    registration = registry.active_registration(conn, caller_id)
    if registration is None:
        return CommandResult(registry.MSG_NOT_AUTHORIZED)
    registry.touch_in_tx(conn, registration["id"], now)  # /settings counts as use; /status doesn't
    if isinstance(private, bool):
        conn.execute(
            "INSERT INTO member_settings (discord_user_id, answers_private, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT (discord_user_id) DO UPDATE SET answers_private = excluded.answers_private, "
            "updated_at = excluded.updated_at",
            (caller_id, int(private), now),
        )
        return CommandResult("Saved. " + _describe_default(private))
    return CommandResult(_describe_default(answers_private(conn, caller_id)))


def _ready(conn: sqlite3.Connection, route: registry.Route, membership: str, now: int) -> str:
    """"Ready", plus the warning when the connector, through which every answer arrives, isn't connected, or else
    when the automation's secret is banned. A live connector adds nothing: the bridge can't see grok.com's side
    (DESIGN.md, Connector status)."""
    member = conn.execute("SELECT id FROM registrations WHERE id = ?", (route.registration_id,)).fetchone()
    if registry.connector_state(conn, member, now) == "disconnected":
        return MSG_MEMBER_READY.removesuffix(".") + ", but " + registry.MSG_CONNECTOR_REFUSAL
    if foreign.automation_banned(conn, route.automation_id):
        return foreign.text(foreign.MSG_BLOCKED_STATUS, membership)
    return MSG_MEMBER_READY


def _describe_default(private: bool) -> str:
    if private:
        return ("Your answers are private by default: only you see them. "
                "Use /ask private:false to share one, or /settings private:false to change the default.")
    return ("Your answers are public by default: everyone in the chat sees them. "
            "Use /ask private:true to hide one, or /settings private:true to change the default.")


def status(conn: sqlite3.Connection, caller_id: str, membership: str = OPERATOR, *, now: int) -> CommandResult:
    """The member and automation states. Never shows a URL or secret."""
    route = registry.resolve_route(conn, caller_id, membership)
    if isinstance(route, registry.Route):
        text = _ready(conn, route, membership, now)
    elif route.message == registry.MSG_NO_AUTOMATION:
        text = MSG_MEMBER_NO_AUTOMATION
    else:
        text = route.message
    member = registry.active_registration(conn, caller_id)
    if member is not None and foreign.warned(conn, member["id"], now):
        text += " " + foreign.MSG_CONNECTOR_USED
    if is_paused(conn):
        text = f"{MSG_PAUSED} {text}"
    return CommandResult(text)
