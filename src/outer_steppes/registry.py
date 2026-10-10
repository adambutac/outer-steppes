"""Members, automations, the route predicate, revoke, and pause.

A member is an active registrations row (the name is kept, since everything
references registration_id). Its automation (today only a grok.com webhook)
lives in the automations table, at most one active per member. Automations are
never updated in place: setting one removes the old row and inserts a new one,
so interactions recorded against the old id fail their re-checks.

Shared by the Discord commands (inside the ACK transaction), the Discord login,
and the admin API. Functions ending in _in_tx must be called inside a transaction.
DESIGN.md: "Route predicate", "Members and automations", "Registration (operator)".
"""

from __future__ import annotations

import base64
import binascii
import re
import sqlite3
import uuid
from collections.abc import Collection
from dataclasses import dataclass

from . import claim, foreign, sessions, url_policy
from .config import OPERATOR, SHARED
from .policy import MSG_PAUSED, is_paused
from . import sweeper  # a module import: sweeper imports oauth, which imports this module

MSG_WITHDRAWN = "This reply was withdrawn."
MSG_PAUSED_WITHDRAWN = "Grok is paused. This request was withdrawn."
MSG_NOT_AUTHORIZED = "Not authorized."
MSG_NOT_MEMBER = "You're not a member yet. Add the Outer Steppes connector in grok.com and sign in with Discord."
MSG_NOT_MEMBER_OPERATOR = "You're not a member yet. Ask the operator to add you."
MSG_NOT_MEMBER_PERSONAL = ("You're not a member of this bridge. If it's yours, claim it with the link from "
                           "`osctl bridge claim`; otherwise ask its operator.")
MSG_NO_AUTOMATION = "You haven't set an automation yet. Run /webhook set."

# The text a member sees on leaving, on Discord and (without the bold markers) on the account page.
# DESIGN.md, Member lifecycle > What leaving does to grok.com.
MSG_LEFT = ("You've left Outer Steppes. Pending answers won't be posted, and runs grok.com already started "
            "aren't stopped. Remove **Outer Steppes** in grok.com's connector settings, and delete your grok.com "
            "automation or rotate its trigger secret if you won't use it again.")
# PR 8d-2, Connector status. The first one is a sentence fragment that callers finish. There is no "connected"
# text: a live token proves only the bridge's side, so no surface claims it (DESIGN.md, Connector status).
MSG_CONNECTOR_DOWN = ("grok.com's connector isn't connected. Add or reconnect **Outer Steppes** in grok.com's "
                      "connector settings")
MSG_CONNECTOR_REFUSAL = MSG_CONNECTOR_DOWN + ", then ask again."

MIN_SECRET_BYTES = 16


class SecretRejected(Exception):
    """The signing secret is malformed. The message never contains the secret."""


class SecretBanned(Exception):
    """The signing secret's key is banned (DESIGN.md, Another member's connector). Never carries the secret."""


class UrlInUse(Exception):
    """Another member's automation holds this trigger URL (DESIGN.md, Members and automations).

    Carries membership row ids for the log line, never the URL: `holder` holds it, `saver` tried to save it
    (`new` when the save would have created the member).
    """

    def __init__(self, holder: str, saver: str) -> None:
        super().__init__("trigger URL in use")
        self.holder, self.saver = holder, saver


BEARER_KEY = re.compile(r"crsr_[0-9a-f]{64}")


def kind_for_url(url: object) -> str | None:
    """The automation kind a trigger URL belongs to, without DNS; None if it matches no provider."""
    try:
        return url_policy.match_provider(url)[0].kind
    except url_policy.UrlRejected:
        return None


def validate_secret(secret: object, kind: str | None) -> None:
    """The secret format for the automation kind: grok.com's `whsec_` + standard base64 of at least 16 bytes, or
    Grok Bot's `crsr_` + 64 hex. With no kind (a URL that matches no provider, refused next), either is accepted."""
    if kind != "grok_webhook" and isinstance(secret, str) and BEARER_KEY.fullmatch(secret):
        return
    if kind == "grok_bot_webhook":
        raise SecretRejected("must be crsr_ followed by 64 hex characters")
    if not isinstance(secret, str) or not secret.startswith("whsec_"):
        raise SecretRejected("must start with whsec_")
    try:
        key = base64.b64decode(secret[len("whsec_"):], validate=True)
    except (binascii.Error, ValueError):
        raise SecretRejected("not valid base64") from None
    if len(key) < MIN_SECRET_BYTES:
        raise SecretRejected(f"key material must be at least {MIN_SECRET_BYTES} bytes")


# ------------------------------------------------------------------ route

@dataclass(frozen=True)
class Route:
    """The caller's own membership: role grants are retired (ADR-0009), so every route is the caller's own."""
    registration_id: str
    owner_id: str            # always the caller; kept for the per-owner rate bucket
    automation_id: str


@dataclass(frozen=True)
class Refusal:
    message: str


def not_member_message(membership: str) -> str:
    """The not-a-member text for the bridge's MEMBERSHIP (DESIGN.md, Route predicate)."""
    return {SHARED: MSG_NOT_MEMBER, OPERATOR: MSG_NOT_MEMBER_OPERATOR}.get(membership, MSG_NOT_MEMBER_PERSONAL)


def resolve_route(conn: sqlite3.Connection, caller_id: str, membership: str = OPERATOR) -> Route | Refusal:
    """The caller's own membership and its active automation, else a refusal.

    `membership` only chooses the not-a-member wording.
    """
    own = conn.execute(
        "SELECT id FROM registrations WHERE discord_user_id = ? AND revoked_at IS NULL", (caller_id,)
    ).fetchone()
    if own:
        automation = active_automation(conn, own["id"])
        if automation is None:
            return Refusal(MSG_NO_AUTOMATION)
        return Route(own["id"], caller_id, automation["id"])
    return Refusal(not_member_message(membership))


def recheck(conn: sqlite3.Connection, row: sqlite3.Row, guild_allowlist: Collection[str]) -> str | None:
    """Re-run the ACK-time decision for an interaction row (DESIGN.md, Route predicate).

    Used by the worker before signing and by deliver_reply before posting.
    Returns the refusal message, or None if the interaction may proceed.
    """
    if is_paused(conn):
        return MSG_PAUSED
    # GUILD_ALLOWLIST restricts only servers that installed the app (DESIGN.md, Install contexts).
    if row["guild_installed"] and row["guild_id"] is not None and guild_allowlist \
            and row["guild_id"] not in guild_allowlist:
        return MSG_NOT_AUTHORIZED
    route = resolve_route(conn, row["caller_id"])
    # The same member and the same automation: replacing or removing the automation
    # changes its id or ends it, which withdraws what was accepted for the old one.
    if not isinstance(route, Route) or route.registration_id != row["registration_id"] \
            or route.automation_id != row["automation_id"]:
        return MSG_NOT_AUTHORIZED
    return None


# ------------------------------------------------------------------ registrations

def active_registration(conn: sqlite3.Connection, discord_user_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM registrations WHERE discord_user_id = ? AND revoked_at IS NULL", (discord_user_id,)
    ).fetchone()


def list_members(conn: sqlite3.Connection) -> list[dict]:
    """Active members, oldest first, for `osctl member list`. No URL, secret, or token ever leaves here."""
    # `id` is the membership row id that log lines carry, so the operator can match them (DESIGN.md, Another
    # member's connector); `secret_banned` says whether the active automation's secret is banned.
    rows = conn.execute(
        "SELECT r.id, r.discord_user_id, r.label, r.created_at, a.kind AS automation, "
        "EXISTS (SELECT 1 FROM banned_secrets b WHERE b.secret_hash = a.secret_hash) AS secret_banned, "
        "COUNT(DISTINCT t.connection_id) AS connections, MAX(t.last_used_at) AS last_used_at "
        "FROM registrations r "
        "LEFT JOIN automations a ON a.registration_id = r.id AND a.removed_at IS NULL "
        "LEFT JOIN oauth_tokens t ON t.registration_id = r.id "
        "WHERE r.revoked_at IS NULL GROUP BY r.id ORDER BY r.created_at, r.discord_user_id").fetchall()
    return [{"id": row["id"], "discord_user_id": row["discord_user_id"], "label": row["label"],
             "created_at": row["created_at"], "automation": row["automation"],
             "secret_banned": bool(row["secret_banned"]), "connections": row["connections"],
             "last_used_at": row["last_used_at"]} for row in rows]


def member_count(conn: sqlite3.Connection) -> int:
    return conn.execute("SELECT count(*) FROM registrations WHERE revoked_at IS NULL").fetchone()[0]


def get_or_create_member_in_tx(conn: sqlite3.Connection, discord_user_id: str, now: int,
                               label: str | None = None) -> tuple[str, bool]:
    """The user's active membership, created (with no automation) if there is none.

    Returns (registration_id, created). Inside the caller's BEGIN IMMEDIATE, so
    two concurrent first logins by the same user make one member; the partial
    unique index registrations_one_active backs that up. Creating a member, by any
    path, claims the bridge, so the claim secret goes (DESIGN.md, Members and automations).
    """
    existing = active_registration(conn, discord_user_id)
    if existing is not None:
        return existing["id"], False
    registration_id = uuid.uuid4().hex
    conn.execute(
        "INSERT INTO registrations (id, discord_user_id, label, created_at, updated_at, last_used_at) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (registration_id, discord_user_id, label, now, now, now),
    )
    claim.clear_in_tx(conn)
    return registration_id, True


def connector_live(conn: sqlite3.Connection, registration_id: str, now: int) -> bool:
    """Whether the member has a live connector connection: any unexpired refresh token."""
    return conn.execute("SELECT 1 FROM oauth_tokens WHERE registration_id = ? AND kind = 'refresh' "
                        "AND expires_at > ? LIMIT 1", (registration_id, now)).fetchone() is not None


def connector_state(conn: sqlite3.Connection, member: sqlite3.Row, now: int) -> str:
    """"connected" or "disconnected". Every member's answers arrive through the connector (ADR-0017), so there is
    no member it doesn't apply to. "connected" is internal: no surface shows it, since a live token proves only the
    bridge's side. DESIGN.md, Connector status."""
    return "connected" if connector_live(conn, member["id"], now) else "disconnected"


def connector_refusal(conn: sqlite3.Connection, registration_id: str, now: int) -> str | None:
    """The /ask refusal at ACK for a member with no live connector connection, including one who never connected:
    nothing is signed, so no run is wasted and nobody waits out the reply window (the PR 8a pilot).

    It reads the member's row itself, inside the ACK transaction, rather than reusing what the route
    returned: the member's active state is checked here, at the moment of the decision.
    """
    member = conn.execute("SELECT id FROM registrations WHERE id = ? AND revoked_at IS NULL",
                          (registration_id,)).fetchone()
    if member is not None and connector_state(conn, member, now) == "disconnected":
        return MSG_CONNECTOR_REFUSAL
    return None


def touch_in_tx(conn: sqlite3.Connection, registration_id: str, now: int) -> None:
    """Renew the member's idle clock (DESIGN.md, Member lifecycle > Idle expiry).

    Called for an accepted /ask, an account-page save, /settings, a connector
    login, and a delivered discord_reply. Never for /status or /webhook status.
    """
    conn.execute("UPDATE registrations SET last_used_at = ? WHERE id = ? AND revoked_at IS NULL",
                 (now, registration_id))


def active_automation(conn: sqlite3.Connection, registration_id: str) -> sqlite3.Row | None:
    return conn.execute(
        "SELECT * FROM automations WHERE registration_id = ? AND removed_at IS NULL", (registration_id,)
    ).fetchone()


def set_automation_in_tx(conn: sqlite3.Connection, registration_id: str, automation_url: str,
                         signing_secret: str, now: int) -> tuple[bool, int]:
    """Set or replace the member's automation. Returns (replaced, withdrawn).

    Removes the old row first, then inserts: the unique indexes automations_one_active and automations_one_url
    are checked per statement, so the member's own URL passes. Re-saving identical values is still a replacement,
    and withdraws the old automation's pending requests. Raises SecretBanned or UrlInUse, checked here, inside
    the save's transaction, so a ban or another member's save that landed meanwhile is caught; the caller's
    transaction then rolls back whole.
    """
    kind = kind_for_url(automation_url)
    if kind is None:  # every caller validates the URL first, so a stored row's kind always matches its URL
        raise ValueError("automation URL matches no provider")
    hashed = foreign.secret_hash(signing_secret)
    if foreign.is_banned(conn, hashed):
        raise SecretBanned()
    holder = conn.execute("SELECT registration_id FROM automations WHERE automation_url = ? "
                          "AND registration_id != ?", (automation_url, registration_id)).fetchone()
    if holder is not None:
        raise UrlInUse(holder["registration_id"], registration_id)
    replaced, withdrawn = remove_automation_in_tx(conn, registration_id, now)
    conn.execute(
        "INSERT INTO automations (id, registration_id, kind, automation_url, signing_secret, secret_hash, "
        "created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
        (uuid.uuid4().hex, registration_id, kind, automation_url, signing_secret, hashed, now),
    )
    conn.execute("UPDATE registrations SET updated_at = ? WHERE id = ?", (now, registration_id))
    return replaced, withdrawn


def remove_automation_in_tx(conn: sqlite3.Connection, registration_id: str, now: int) -> tuple[bool, int]:
    """Wipe and end the member's automation, and withdraw its in-flight work.

    Returns (removed, withdrawn). Idempotent: with no active automation it does
    nothing. The member, their /settings, and their connector connections stay.
    """
    automation = active_automation(conn, registration_id)
    if automation is None:
        return False, 0
    conn.execute(
        "UPDATE automations SET removed_at = ?, automation_url = NULL, signing_secret = NULL, secret_hash = NULL "
        "WHERE id = ?",
        (now, automation["id"]),
    )
    withdrawn = _withdraw_in_tx(
        conn, "SELECT interaction_id FROM interactions WHERE automation_id = ? "
              "AND status IN ('queued', 'posted')", (automation["id"],), MSG_WITHDRAWN, now)
    return True, withdrawn


def upsert_registration_in_tx(conn: sqlite3.Connection, discord_user_id: str, automation_url: str,
                              signing_secret: str, label: str | None, now: int) -> dict:
    """The operator's POST /v1/registrations: the member (created if needed) and a new automation.

    Always a new automation row (see set_automation_in_tx).
    """
    registration_id, created = get_or_create_member_in_tx(conn, discord_user_id, now, label)
    if not created:
        conn.execute("UPDATE registrations SET label = ?, updated_at = ? WHERE id = ?", (label, now, registration_id))
    try:
        replaced, withdrawn = set_automation_in_tx(conn, registration_id, automation_url, signing_secret, now)
    except UrlInUse as exc:
        if created:  # the rollback takes the new member with it, so there is no id to log
            raise UrlInUse(exc.holder, "new") from None
        raise
    return {"id": registration_id, "discord_user_id": discord_user_id, "label": label,
            "created": created, "replaced": replaced, "withdrawn": withdrawn}


def revoke_in_tx(conn: sqlite3.Connection, registration_id: str, now: int) -> int:
    """End the membership and withdraw in-flight work. Returns how many interactions were withdrawn.

    "Withdrawn" refuses the answer; it cannot cancel a grok.com run that a
    committed POST already started (DESIGN.md, What "withdrawn" means).
    """
    conn.execute(
        "UPDATE registrations SET revoked_at = ?, updated_at = ? WHERE id = ? AND revoked_at IS NULL",
        (now, now, registration_id),
    )
    # A session never outlives the membership (DESIGN.md, The account page), whichever
    # path leaves: /leave, the account page, the operator, or idle expiry.
    member = conn.execute("SELECT discord_user_id FROM registrations WHERE id = ?", (registration_id,)).fetchone()
    if member is not None:
        sessions.delete_for_user_in_tx(conn, member["discord_user_id"])
    # The automation's URL and secret are wiped, so the bridge can never sign it again.
    conn.execute(
        "UPDATE automations SET removed_at = ?, automation_url = NULL, signing_secret = NULL, secret_hash = NULL "
        "WHERE registration_id = ? AND removed_at IS NULL", (now, registration_id),
    )
    # Every connector credential goes with the registration: issued clients, and
    # the codes and tokens of any client, including the shared public one (they're
    # bound to the registration, not to an oauth_clients row).
    conn.execute("DELETE FROM oauth_clients WHERE registration_id = ?", (registration_id,))
    conn.execute("DELETE FROM oauth_codes WHERE registration_id = ?", (registration_id,))
    conn.execute("DELETE FROM oauth_tokens WHERE registration_id = ?", (registration_id,))
    return _withdraw_in_tx(
        conn, "SELECT interaction_id FROM interactions WHERE registration_id = ? "
              "AND status IN ('queued', 'posted')", (registration_id,), MSG_WITHDRAWN, now)


def _withdraw_in_tx(conn: sqlite3.Connection, query: str, params: tuple, message: str, now: int) -> int:
    withdrawn = 0
    for row in conn.execute(query, params).fetchall():
        if sweeper.finalize_in_tx(conn, row["interaction_id"], ("queued", "posted"), "failed", message, now):
            withdrawn += 1
    return withdrawn


# ------------------------------------------------------------------ pause

def set_paused_in_tx(conn: sqlite3.Connection, paused: bool, now: int) -> int:
    """Set the kill switch. Pausing withdraws every queued and posted interaction."""
    conn.execute(
        "INSERT INTO settings (key, value) VALUES ('paused', ?) "
        "ON CONFLICT (key) DO UPDATE SET value = excluded.value",
        ("1" if paused else "0",),
    )
    if not paused:
        return 0
    return _withdraw_in_tx(
        conn, "SELECT interaction_id FROM interactions WHERE status IN ('queued', 'posted')", (),
        MSG_PAUSED_WITHDRAWN, now)


def pending_final_messages(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT count(*) FROM interactions WHERE final_message IS NOT NULL AND interaction_token IS NOT NULL"
    ).fetchone()[0]
