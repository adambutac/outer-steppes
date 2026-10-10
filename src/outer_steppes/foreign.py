"""A run that calls through another member's connector (DESIGN.md, Connector status > Another member's connector;
ADR-0018).

The bridge knows the reply token's owner (from the row) and the connector's member (from the access token), but not
which trigger started the run. So the foreign ending bans only the token owner's signing secret, and only warns the
connector's member: a token fed into someone else's run then bans only whoever fed it.

Bans are keyed on the SHA-256 of the secret's decoded key, so a re-encoded copy of a banned key is refused too, and
every check compares hashes: nothing here reads a plaintext secret except secret_hash() at save time.
Functions ending in _in_tx must be called inside a transaction.
"""

from __future__ import annotations

import base64
import hashlib
import sqlite3

from . import sweeper  # a module import: sweeper imports oauth, which imports registry, which imports this module
from .config import OPERATOR

# How long the connector's member is warned, unless they connect again first (DESIGN.md: a week is long enough to be
# seen by a member who checks now and then; it needs no action unless their connector is in the wrong account).
WARNING_SECONDS = 7 * 86400

_SAVE_SELF = "and save it on the account page"
_SAVE_OPERATOR = "and ask the operator to save it"
_FIX = ("In the grok.com account that owns your automation, keep only your own **Outer Steppes** connector, "
        "connected while signed in to Discord as yourself. Then rotate the automation's signing secret in grok.com, "
        "or create a new automation there, {save}")
_BLOCKED_WHY = ("signing secret is blocked, because a run of it used an **Outer Steppes** connector that belongs to "
                "a different Discord account. ")

MSG_FOREIGN_ENDED = ("Grok's run used an **Outer Steppes** connector that belongs to a different Discord account, so "
                     "this request was stopped and your automation's signing secret is blocked. " + _FIX
                     + " before you ask again.")
MSG_BLOCKED = "Your automation's " + _BLOCKED_WHY + _FIX + ", then ask again."
MSG_BLOCKED_STATUS = "You're a member and your automation is set, but its " + _BLOCKED_WHY + _FIX + "."
MSG_BLOCKED_NOTE = "Your automation's " + _BLOCKED_WHY + _FIX + "."
MSG_SECRET_BANNED = ("That signing secret is blocked, because a run of its automation used an Outer Steppes connector "
                     "that belongs to a different Discord account. Rotate the secret in grok.com, or create a new "
                     "automation, and save that.")
MSG_CONNECTOR_USED = ("Your **Outer Steppes** connector was used by a run with another member's request, which was "
                      "stopped. If your connector is in a grok.com account that isn't yours, remove it there and "
                      "connect again from your own. Nothing of yours is blocked.")
MSG_URL_IN_USE = ("That trigger URL is already in use on this bridge. Create a new automation in grok.com and save "
                  "its trigger URL and secret.")


def text(message: str, membership: str) -> str:
    """A member-facing text for the bridge's MEMBERSHIP: with `operator`, members can't save on the account page."""
    return message.format(save=_SAVE_OPERATOR if membership == OPERATOR else _SAVE_SELF)


def secret_hash(signing_secret: str) -> str:
    """SHA-256 of the key bytes, base64-decoded from what follows `whsec_`, or of a Grok Bot `crsr_` key as it
    stands, which has only one spelling (the secret is already validated)."""
    if signing_secret.startswith("crsr_"):
        return hashlib.sha256(signing_secret.encode("ascii")).hexdigest()
    return hashlib.sha256(base64.b64decode(signing_secret[len("whsec_"):])).hexdigest()


def is_banned(conn: sqlite3.Connection, hashed: str) -> bool:
    return conn.execute("SELECT 1 FROM banned_secrets WHERE secret_hash = ?", (hashed,)).fetchone() is not None


def automation_banned(conn: sqlite3.Connection, automation_id: str) -> bool:
    """Whether this active automation's secret is banned."""
    return conn.execute(
        "SELECT 1 FROM automations a JOIN banned_secrets b ON b.secret_hash = a.secret_hash "
        "WHERE a.id = ? AND a.removed_at IS NULL", (automation_id,)).fetchone() is not None


def warned(conn: sqlite3.Connection, registration_id: str, now: int) -> bool:
    """Whether the member's connector made a foreign call in the last week with no new connection since."""
    row = conn.execute("SELECT foreign_use_at FROM registrations WHERE id = ? AND revoked_at IS NULL",
                       (registration_id,)).fetchone()
    if row is None or row["foreign_use_at"] is None or now >= row["foreign_use_at"] + WARNING_SECONDS:
        return False
    return conn.execute("SELECT 1 FROM oauth_tokens WHERE registration_id = ? AND connected_at > ? LIMIT 1",
                        (registration_id, row["foreign_use_at"])).fetchone() is None


def foreign_ending_in_tx(conn: sqlite3.Connection, row: sqlite3.Row, caller_registration_id: str, membership: str,
                         now: int) -> bool:
    """T11b, for a call whose access token isn't the row's member. Returns whether it applied.

    Only a `posted` row inside its reply window: a token with no row names nobody, an ended row's token is no
    longer the live run's, and an `answering` row is already claimed by its owner's connector.
    """
    if row["status"] != "posted" or row["reply_expires_at"] <= now:
        return False
    if not sweeper.finalize_in_tx(conn, row["interaction_id"], ("posted",), "failed", text(MSG_FOREIGN_ENDED, membership),
                          now):
        return False
    conn.execute("INSERT OR IGNORE INTO banned_secrets (secret_hash, banned_at) "
                 "SELECT secret_hash, ? FROM automations WHERE id = ? AND removed_at IS NULL", (now, row["automation_id"]))
    conn.execute("UPDATE registrations SET foreign_use_at = ? WHERE id = ? AND revoked_at IS NULL",
                 (now, caller_registration_id))
    return True
