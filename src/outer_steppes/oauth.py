"""OAuth 2.1 for the grok.com custom connector (DESIGN.md, OAuth for the grok.com connector).

Two kinds of client:

  issued   one per registration, from `osctl connector client add` (PR 5). Holding the
           client secret is what authorizes it, so authorize issues a code at once.
  public   OAUTH_PUBLIC_CLIENT_ID, shared by every member (PR 8a). It exists only
           when Discord login is configured. Authorize detours through Discord
           OAuth2 (identify); the code is bound to the registration of the Discord
           user who signed in. Token auth method "none" (PKCE only), or an
           optional shared, non-secret OAUTH_PUBLIC_CLIENT_SECRET.

The grok.com side, as observed by the probes:

  authorize  browser redirect; fixed callback URL; PKCE S256; resource = /mcp URL
  token      server-to-server; client_secret_post, basic, or none;
             authorization_code with code_verifier, and refresh_token

Every secret (client secret, code, access and refresh tokens, login state and
browser nonce) is stored only as its SHA-256. Codes and tokens name their
registration directly. Access tokens alone cannot post anything: every
discord_reply call also needs a live reply token routed to the same registration.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import re
import secrets
import sqlite3
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol
from urllib.parse import unquote, urlencode, urlsplit

from . import claim, metrics, registry, sessions
from .config import OPERATOR, PERSONAL, Config
from .db import transaction
from .discord_login import DISCORD_AUTHORIZE, DiscordLoginError, DiscordUser
from .policy import is_paused

log = logging.getLogger(__name__)

CLIENT_ID_PREFIX = "osc_"
CLIENT_SECRET_PREFIX = "oss_"
ACCESS_TOKEN_PREFIX = "mcp_"
REFRESH_TOKEN_PREFIX = "mcpr_"
CODE_TTL_SECONDS = 60
# For a token the bridge no longer knows. It names both causes because leaving deletes the tokens, so the
# bridge can't tell a left member's token from an expired or rotated one (DESIGN.md, Member lifecycle).
# It is put into a WWW-Authenticate header without escaping, so it must never contain a double quote or a
# line break (a test checks this).
MSG_CONNECTION_INVALID = ("This connection is no longer valid: the Outer Steppes membership ended or the "
                          "connection expired. Reconnect in grok.com to join again.")
PENDING_TTL_SECONDS = 600
SCOPE = "mcp"
# __Host-: Secure, Path=/, and no Domain, so a sibling subdomain can't plant its own pending login and
# have the victim's Discord sign-in complete it (DESIGN.md, Threat model).
LOGIN_COOKIE = "__Host-os_login"
DISCORD_CALLBACK_PATH = "/oauth/discord/callback"

MSG_BAD_CALLBACK = "This callback URL is not allowed for this bridge."
MSG_UNKNOWN_CLIENT = "Unknown or revoked client. Ask the operator for a new one."
MSG_TOO_MANY_PENDING = "Too many sign-ins are in progress. Try again in a few minutes."
MSG_BAD_STATE = ("This sign-in link is invalid, expired, or was already used, or it was opened in a different "
                 "browser. Start again from grok.com's connector settings.")
MSG_NOT_REGISTERED = ("Signed in to Discord as {name}, but this bridge can't add you: only the operator adds "
                      "members here. Ask the operator to add you, then connect again.")
MSG_NOT_ALLOWED = "Signed in to Discord as {name}, but this bridge can't add you: that account isn't allowed here."
MSG_FULL = ("Signed in to Discord as {name}, but this bridge can't add you: it has no room for new members. "
            "Ask the operator.")
MSG_PAUSED = "Signed in to Discord as {name}, but the bridge is paused. Try again later."
MSG_UNCLAIMED = ("Signed in to Discord as {name}, but this bridge can't add you: it hasn't been claimed yet. "
                 "Its owner claims it with the link from osctl bridge claim.")
MSG_PERSONAL = "Signed in to Discord as {name}, but this bridge can't add you: it's personal to its owner. Ask the operator."
# The claim link (DESIGN.md, Claiming the bridge). Pages render no markdown, so these have no backticks.
MSG_CLAIM_LINK_INVALID = ("This claim link is invalid, expired, or was already used. Run osctl bridge claim "
                          "for a new one.")
MSG_CLAIM_INVALID = ("Signed in to Discord as {name}, but this claim link is no longer valid: it expired, was "
                     "replaced, or the bridge was already claimed. Ask the operator for a new one.")
# Account-page logins (PR 8c). These pages link back to /account.
ACCOUNT_PATH = "/account"
MSG_SIGN_IN_CANCELLED = "Sign-in cancelled."
MSG_DISCORD_DOWN = "Discord didn't answer; try again."
# The connect-time warning (DESIGN.md, Connect-time warning): one paragraph per item, kept equal to
# DESIGN.md's quote by a test.
CONNECT_WARNING = (
    "You're about to connect your grok.com account to an Outer Steppes bridge. Whoever runs this bridge will be "
    "able to steer Grok in any chat or automation that uses this connector, and Grok can act through your other "
    "connected apps there: email, cloud storage, bank accounts, or anything else you've connected. They also see "
    "every prompt and answer that passes through the bridge.",
    "We advise against connecting to a bridge someone else runs: run your own instead. Only continue if this is "
    "your own bridge or you fully trust whoever runs it.",
    "Are you sure you want to continue?",
)
MSG_CONNECT_NEXT = "Continuing takes you to Discord to sign in, so this bridge knows which Discord account is connecting."

_PKCE = re.compile(r"[A-Za-z0-9\-._~]{43,128}")


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def pkce_s256(verifier: str) -> str:
    return base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode()


class Identifier(Protocol):
    """Resolves a Discord authorization code to the signed-in user (discord_login.DiscordLogin)."""

    async def identify(self, code: str) -> DiscordUser: ...


# ------------------------------------------------------------------ clients and connections (operator)

def issue_client_in_tx(conn: sqlite3.Connection, registration_id: str, now: int) -> tuple[str, str]:
    """A new client for a registration. The secret is returned once and stored hashed."""
    client_id = CLIENT_ID_PREFIX + secrets.token_urlsafe(16)
    client_secret = CLIENT_SECRET_PREFIX + secrets.token_urlsafe(32)
    conn.execute("INSERT INTO oauth_clients (client_id, registration_id, secret_hash, created_at) VALUES (?, ?, ?, ?)",
                 (client_id, registration_id, _hash(client_secret), now))
    return client_id, client_secret


def list_clients(conn: sqlite3.Connection, registration_id: str) -> list[dict]:
    rows = conn.execute("SELECT client_id, created_at, last_used_at FROM oauth_clients WHERE registration_id = ? "
                        "ORDER BY created_at", (registration_id,)).fetchall()
    return [dict(row) for row in rows]


def delete_client_in_tx(conn: sqlite3.Connection, client_id: str) -> bool:
    """Revoke one issued client. Its codes and tokens go with it (they don't cascade from oauth_clients)."""
    deleted = conn.execute("DELETE FROM oauth_clients WHERE client_id = ?", (client_id,)).rowcount == 1
    if deleted:
        conn.execute("DELETE FROM oauth_codes WHERE client_id = ?", (client_id,))
        conn.execute("DELETE FROM oauth_tokens WHERE client_id = ?", (client_id,))
    return deleted


def list_connections(conn: sqlite3.Connection, registration_id: str) -> list[dict]:
    """One entry per connection. connected_at and last_used_at are carried across refresh rotation."""
    rows = conn.execute(
        "SELECT connection_id, MIN(client_id) AS client_id, MIN(connected_at) AS created_at, "
        "MAX(last_used_at) AS last_used_at FROM oauth_tokens WHERE registration_id = ? "
        "GROUP BY connection_id ORDER BY created_at, connection_id", (registration_id,)).fetchall()
    return [{"connection_id": row["connection_id"],
             "client_kind": "issued" if row["client_id"].startswith(CLIENT_ID_PREFIX) else "public",
             "created_at": row["created_at"], "last_used_at": row["last_used_at"]} for row in rows]


def delete_connections_in_tx(conn: sqlite3.Connection, registration_id: str, connection_id: str | None = None) -> int:
    """Disconnect one connection, or all of them. Returns how many connections were removed."""
    if connection_id is None:
        count = conn.execute("SELECT COUNT(DISTINCT connection_id) FROM oauth_tokens WHERE registration_id = ?",
                             (registration_id,)).fetchone()[0]
        conn.execute("DELETE FROM oauth_tokens WHERE registration_id = ?", (registration_id,))
        return count
    return 1 if conn.execute("DELETE FROM oauth_tokens WHERE registration_id = ? AND connection_id = ?",
                             (registration_id, connection_id)).rowcount else 0


def delete_expired_in_tx(conn: sqlite3.Connection, now: int) -> int:
    codes = conn.execute("DELETE FROM oauth_codes WHERE expires_at <= ?", (now,)).rowcount
    tokens = conn.execute("DELETE FROM oauth_tokens WHERE expires_at <= ?", (now,)).rowcount
    pending = conn.execute("DELETE FROM oauth_pending WHERE expires_at <= ?", (now,)).rowcount
    return codes + tokens + pending


def connector_settings(config: Config) -> dict[str, str]:
    """What every member enters in grok.com for the shared public client. Nothing here is a credential."""
    base = config.public_base_url
    return {
        "connector_name": "Outer Steppes",
        "mcp_server_url": base + "/mcp",
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "client_id": config.oauth_public_client_id,
        "client_secret": config.oauth_public_client_secret,
        "scopes": SCOPE,
        "token_auth_method": "client_secret_post" if config.oauth_public_client_secret else "none",
        "discord_redirect_url": base + DISCORD_CALLBACK_PATH,
    }


# ------------------------------------------------------------------ results

@dataclass(frozen=True)
class AuthorizeResult:
    """A redirect (to the client's callback or to Discord), or a page (no redirect).

    `page` is plain text; the HTTP layer escapes it and adds the page headers.
    `link_to_account` adds a link back to the account page under it.
    `login_cookie`, when set, is the browser nonce for the __Host-os_login cookie;
    `session_cookie`, a new account-page session id.
    `continue_url` and `cancel_url`, when set, make it the connect-time warning page
    instead: Continue goes to Discord, Cancel back to the client's callback.
    """
    status: int
    location: str | None = None
    page: str | None = None
    login_cookie: str | None = None
    session_cookie: str | None = None
    link_to_account: bool = False
    continue_url: str | None = None
    cancel_url: str | None = None


@dataclass(frozen=True)
class TokenResult:
    status: int
    body: dict
    www_authenticate: str | None = None


class OAuthServer:
    def __init__(self, conn: sqlite3.Connection, config: Config, clock: Callable[[], float] = time.time) -> None:
        self._conn = conn
        self._config = config
        self._clock = clock

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn

    @property
    def public_base_url(self) -> str:
        return self._config.public_base_url

    @property
    def resource(self) -> str:
        return self._config.public_base_url + "/mcp"

    @property
    def discord_redirect_uri(self) -> str:
        return self._config.public_base_url + DISCORD_CALLBACK_PATH

    def _is_public(self, client_id: str | None) -> bool:
        return self._config.discord_login_enabled and client_id == self._config.oauth_public_client_id

    # -------------------------------------------------------------- authorize
    def authorize(self, params: Mapping[str, str]) -> AuthorizeResult:
        client_id, redirect_uri = params.get("client_id", ""), params.get("redirect_uri", "")
        # RFC 6749 4.1.2.1: with a bad client or callback, never redirect.
        if redirect_uri not in self._config.oauth_redirect_uris:
            return AuthorizeResult(400, page=MSG_BAD_CALLBACK)
        public = self._is_public(client_id)
        client = None if public else self._active_client(client_id)
        if not public and client is None:
            return AuthorizeResult(400, page=MSG_UNKNOWN_CLIENT)

        def fail(error: str) -> AuthorizeResult:
            return AuthorizeResult(302, location=_with_query(redirect_uri, error=error, state=params.get("state")))

        if params.get("response_type") != "code":
            return fail("unsupported_response_type")
        if params.get("code_challenge_method") != "S256" or not _PKCE.fullmatch(params.get("code_challenge", "")):
            return fail("invalid_request")
        resource = params.get("resource")
        if resource is not None and resource != self.resource:
            return fail("invalid_target")
        if not set(params.get("scope", "").split()) <= {SCOPE}:
            return fail("invalid_scope")

        now = int(self._clock())
        if public:
            return self._start_discord_login("connector", params, now)
        code = secrets.token_urlsafe(32)
        with transaction(self._conn):
            self._conn.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, registration_id, redirect_uri, code_challenge, "
                "resource, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_hash(code), client_id, client["registration_id"], redirect_uri, params["code_challenge"],
                 resource, now + CODE_TTL_SECONDS))
        return AuthorizeResult(302, location=_with_query(redirect_uri, code=code, state=params.get("state")))

    def start_account_login(self) -> AuthorizeResult:
        """GET /account/login: sign in with Discord for the account page. No grok.com request is involved.

        Stored as purpose 'register': this login can register a new member.
        """
        return self._start_discord_login("register", {}, int(self._clock()))

    def start_claim_login(self, token: str) -> AuthorizeResult:
        """GET /account/claim: sign in with Discord to claim the bridge (DESIGN.md, Claiming the bridge).

        Only for the current, unexpired link on an unclaimed bridge; anything else
        writes nothing. The pending row keeps the link's hash, so the callback can
        tell whether it is still the current one.
        """
        now = int(self._clock())
        presented = claim.secret_hash(token)
        if self._config.membership == OPERATOR or not claim.hash_is_current(self._conn, presented, now):
            return AuthorizeResult(400, page=MSG_CLAIM_LINK_INVALID)
        return self._start_discord_login("claim", {}, now, claim_hash=presented)

    def _start_discord_login(self, purpose: str, params: Mapping[str, str], now: int,
                             claim_hash: str | None = None) -> AuthorizeResult:
        """Store the pending login (with grok.com's request, for a connector) and send the browser to Discord."""
        state, nonce = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        with transaction(self._conn):
            # Only unexpired rows count; the sweeper deletes expired ones.
            pending = self._conn.execute("SELECT COUNT(*) FROM oauth_pending WHERE expires_at > ?",
                                         (now,)).fetchone()[0]
            if pending >= self._config.oauth_max_pending:
                return AuthorizeResult(503, page=MSG_TOO_MANY_PENDING)
            self._conn.execute(
                "INSERT INTO oauth_pending (state_hash, nonce_hash, purpose, redirect_uri, code_challenge, resource, "
                "client_state, claim_hash, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (_hash(state), _hash(nonce), purpose, params.get("redirect_uri"), params.get("code_challenge"),
                 params.get("resource"), params.get("state"), claim_hash, now + PENDING_TTL_SECONDS))
        location = DISCORD_AUTHORIZE + "?" + urlencode({
            "response_type": "code", "client_id": self._config.discord_application_id, "scope": "identify",
            "redirect_uri": self.discord_redirect_uri, "state": state})
        if purpose == "connector":
            # Stop on the connect-time warning before Discord (DESIGN.md, Connect-time warning). Cancel answers
            # grok.com as Discord's own cancel does; its callback was checked against OAUTH_REDIRECT_URIS.
            cancel = _with_query(params["redirect_uri"], error="access_denied", state=params.get("state"))
            return AuthorizeResult(200, login_cookie=nonce, continue_url=location, cancel_url=cancel)
        return AuthorizeResult(302, location=location, login_cookie=nonce)

    # -------------------------------------------------------------- Discord callback
    async def discord_callback(self, query: Mapping[str, str], cookie: str | None,
                               identifier: Identifier) -> AuthorizeResult:
        """GET /oauth/discord/callback (DESIGN.md, the outcomes table).

        The pending row is consumed by the first callback that names it,
        whatever the outcome. Without a matching state and browser cookie there
        is no trustworthy grok.com request to answer, so nothing redirects.
        """
        now = int(self._clock())
        with transaction(self._conn):
            row = self._conn.execute("SELECT * FROM oauth_pending WHERE state_hash = ?",
                                     (_hash(query.get("state", "")),)).fetchone()
            if row is not None:
                self._conn.execute("DELETE FROM oauth_pending WHERE state_hash = ?", (row["state_hash"],))
        if (row is None or row["expires_at"] <= now or not cookie
                or not hmac.compare_digest(_hash(cookie), row["nonce_hash"])):
            purpose = row["purpose"] if row is not None else "connector"
            return self._login_result(purpose, "bad_state", AuthorizeResult(400, page=MSG_BAD_STATE))
        if row["purpose"] == "register":
            return await self._account_callback(query, identifier, now)
        if row["purpose"] == "claim":
            return await self._claim_callback(query, identifier, now, row["claim_hash"])

        def to_grok(**params: str | None) -> AuthorizeResult:
            return AuthorizeResult(302, location=_with_query(row["redirect_uri"], **params, state=row["client_state"]))

        if "error" in query or not query.get("code"):
            return self._login_result("connector", "denied", to_grok(error="access_denied"))
        try:
            user = await identifier.identify(query["code"])
        except DiscordLoginError:
            return self._login_result("connector", "discord_error", to_grok(error="temporarily_unavailable"))

        name = user.username or user.id
        code = secrets.token_urlsafe(32)
        joined = False

        def refuse(result: str, message: str) -> AuthorizeResult:
            return self._login_result("connector", result, AuthorizeResult(403, page=message.format(name=name)))

        # The outcome rows in DESIGN.md's order; the first that matches decides. The
        # allowlist comes before pause so that (from 8c) an existing member can still
        # sign in to the account page while paused. One BEGIN IMMEDIATE covers the
        # member lookup, the cap, and the creation, so concurrent first logins by the
        # same user make one member and concurrent joins can't exceed MAX_MEMBERS.
        # (In this single-process, single-connection design the event loop already
        # serializes this block, which has no await; the transaction is what keeps it
        # correct if that ever changes, and the unique index backs up the one-member rule.)
        with transaction(self._conn):
            if self._config.user_allowlist and user.id not in self._config.user_allowlist:
                return refuse("not_allowed", MSG_NOT_ALLOWED)
            # A connector login is refused while paused, member or not. (The
            # account-page login differs: an existing member may still sign in.)
            if is_paused(self._conn):
                return refuse("paused", MSG_PAUSED)
            registration = registry.active_registration(self._conn, user.id)
            if registration is not None:
                registration_id = registration["id"]
            else:
                refusal = self._join_refusal()
                if refusal is not None:
                    return refuse(*refusal)
                registration_id, joined = registry.get_or_create_member_in_tx(self._conn, user.id, now)
            registry.touch_in_tx(self._conn, registration_id, now)  # a connector login is use
            self._conn.execute(
                "INSERT INTO oauth_codes (code_hash, client_id, registration_id, redirect_uri, code_challenge, "
                "resource, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_hash(code), self._config.oauth_public_client_id, registration_id, row["redirect_uri"],
                 row["code_challenge"], row["resource"], now + CODE_TTL_SECONDS))
        if joined:
            metrics.METRICS.membership_changes.inc("self", "joined")
            log.info("member joined through the connector login")
        return self._login_result("connector", "ok", to_grok(code=code))

    async def _account_callback(self, query: Mapping[str, str], identifier: Identifier,
                                now: int) -> AuthorizeResult:
        """The account-page column of the outcomes table: a bridge session, then 303 to /account.

        Never a redirect off the bridge. The same rows as the connector login,
        except that an existing member signs in while paused, so that removing
        the automation and leaving stay reachable (saving is refused on the page).
        """
        if "error" in query or not query.get("code"):
            return self._login_result("register", "denied",
                                      AuthorizeResult(400, page=MSG_SIGN_IN_CANCELLED, link_to_account=True))
        try:
            user = await identifier.identify(query["code"])
        except DiscordLoginError:
            return self._login_result("register", "discord_error",
                                      AuthorizeResult(503, page=MSG_DISCORD_DOWN, link_to_account=True))

        name = user.username or user.id
        joined = False

        def refuse(result: str, message: str) -> AuthorizeResult:
            return self._login_result("register", result, AuthorizeResult(403, page=message.format(name=name)))

        # One BEGIN IMMEDIATE, as in the connector login: the lookup, the cap, the
        # creation, and the session.
        with transaction(self._conn):
            if self._config.user_allowlist and user.id not in self._config.user_allowlist:
                return refuse("not_allowed", MSG_NOT_ALLOWED)
            registration = registry.active_registration(self._conn, user.id)
            if registration is None:
                if is_paused(self._conn):
                    return refuse("paused", MSG_PAUSED)
                refusal = self._join_refusal()
                if refusal is not None:
                    return refuse(*refusal)
                _, joined = registry.get_or_create_member_in_tx(self._conn, user.id, now)
            session_id = sessions.create_in_tx(self._conn, user.id, user.username or "", now)
        if joined:
            metrics.METRICS.membership_changes.inc("self", "joined")
            log.info("member joined through the account page")
        return self._login_result("register", "ok", AuthorizeResult(
            303, location=self._config.public_base_url + ACCOUNT_PATH, session_cookie=session_id))

    def _join_refusal(self) -> tuple[str, str] | None:
        """Why a non-member can't join by signing in, in the outcomes table's order, or None.

        Inside the caller's BEGIN IMMEDIATE. Only a claimed bridge with MEMBERSHIP=shared
        takes members this way: the first member comes from the claim link or the operator.
        """
        if self._config.membership == OPERATOR:
            return "not_registered", MSG_NOT_REGISTERED
        if not claim.is_claimed(self._conn):
            return "unclaimed", MSG_UNCLAIMED
        if self._config.membership == PERSONAL:
            return "personal", MSG_PERSONAL
        if registry.member_count(self._conn) >= self._config.max_members:
            metrics.METRICS.membership_changes.inc("self", "full")
            return "full", MSG_FULL
        return None

    async def _claim_callback(self, query: Mapping[str, str], identifier: Identifier, now: int,
                              claim_hash: str) -> AuthorizeResult:
        """A claim login's callback (DESIGN.md, Claiming the bridge): the first member, then the account page."""
        if "error" in query or not query.get("code"):
            return self._login_result("claim", "denied",
                                      AuthorizeResult(400, page=MSG_SIGN_IN_CANCELLED, link_to_account=True))
        try:
            user = await identifier.identify(query["code"])
        except DiscordLoginError:
            return self._login_result("claim", "discord_error",
                                      AuthorizeResult(503, page=MSG_DISCORD_DOWN, link_to_account=True))

        name = user.username or user.id

        def refuse(result: str, message: str) -> AuthorizeResult:
            return self._login_result("claim", result, AuthorizeResult(403, page=message.format(name=name)))

        # One BEGIN IMMEDIATE: the claim is checked and used up, and the member created, together,
        # so two claim logins racing for one link make one member.
        with transaction(self._conn):
            if self._config.user_allowlist and user.id not in self._config.user_allowlist:
                return refuse("not_allowed", MSG_NOT_ALLOWED)
            joined = registry.active_registration(self._conn, user.id) is None
            if joined:
                if is_paused(self._conn):
                    return refuse("paused", MSG_PAUSED)
                # The link that started this login must still be the current one, unexpired,
                # on an unclaimed bridge: a replaced or used link fails even mid-login.
                if self._config.membership == OPERATOR or not claim.hash_is_current(self._conn, claim_hash, now):
                    return refuse("claim_invalid", MSG_CLAIM_INVALID)
                registry.get_or_create_member_in_tx(self._conn, user.id, now)  # also deletes the claim secret
            session_id = sessions.create_in_tx(self._conn, user.id, user.username or "", now)
        if joined:
            metrics.METRICS.membership_changes.inc("self", "claimed")
            log.info("the bridge was claimed")
        return self._login_result("claim", "claimed" if joined else "ok", AuthorizeResult(
            303, location=self._config.public_base_url + ACCOUNT_PATH, session_cookie=session_id))

    @staticmethod
    def _login_result(purpose: str, result: str, outcome: AuthorizeResult) -> AuthorizeResult:
        metrics.METRICS.discord_logins.inc(purpose, result)
        log.info("discord login (%s): %s", purpose, result)
        return outcome

    # -------------------------------------------------------------- token
    def token(self, form: Mapping[str, str], authorization: str | None) -> TokenResult:
        client_id = self._authenticate_client(form, authorization)
        if client_id is None:
            return TokenResult(401, {"error": "invalid_client"}, www_authenticate='Basic realm="outer-steppes"')
        grant = form.get("grant_type")
        if grant == "authorization_code":
            return self._exchange_code(client_id, form)
        if grant == "refresh_token":
            return self._refresh(client_id, form)
        return TokenResult(400, {"error": "unsupported_grant_type"})

    def _exchange_code(self, client_id: str, form: Mapping[str, str]) -> TokenResult:
        code, verifier = form.get("code", ""), form.get("code_verifier", "")
        now = int(self._clock())
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT c.* FROM oauth_codes AS c JOIN registrations AS r ON r.id = c.registration_id "
                "WHERE c.code_hash = ? AND r.revoked_at IS NULL", (_hash(code),)).fetchone()
            # Single use: the code is gone after this attempt, whatever its outcome.
            self._conn.execute("DELETE FROM oauth_codes WHERE code_hash = ?", (_hash(code),))
            if (row is None or row["client_id"] != client_id or row["expires_at"] <= now
                    or row["redirect_uri"] != form.get("redirect_uri")
                    or not _PKCE.fullmatch(verifier)
                    or not hmac.compare_digest(pkce_s256(verifier), row["code_challenge"])
                    or (form.get("resource") is not None and form.get("resource") != (row["resource"] or self.resource))):
                return TokenResult(400, {"error": "invalid_grant"})
            return self._issue_tokens(client_id, row["registration_id"], "con_" + secrets.token_hex(8), now, now)

    def _refresh(self, client_id: str, form: Mapping[str, str]) -> TokenResult:
        now = int(self._clock())
        with transaction(self._conn):
            row = self._conn.execute(
                "SELECT t.* FROM oauth_tokens AS t JOIN registrations AS r ON r.id = t.registration_id "
                "WHERE t.token_hash = ? AND t.kind = 'refresh' AND r.revoked_at IS NULL",
                (_hash(form.get("refresh_token", "")),)).fetchone()
            if row is None or row["client_id"] != client_id or row["expires_at"] <= now:
                return TokenResult(400, {"error": "invalid_grant", "error_description": MSG_CONNECTION_INVALID})
            # Rotation: each refresh token works once. The connection, and when it
            # was first connected, carry over.
            self._conn.execute("DELETE FROM oauth_tokens WHERE token_hash = ?", (row["token_hash"],))
            return self._issue_tokens(client_id, row["registration_id"], row["connection_id"], row["connected_at"],
                                      now)

    def _issue_tokens(self, client_id: str, registration_id: str, connection_id: str, connected_at: int,
                      now: int) -> TokenResult:
        """Inside the caller's transaction. Issuing (or refreshing) counts as use of the connection."""
        access = ACCESS_TOKEN_PREFIX + secrets.token_urlsafe(32)
        refresh = REFRESH_TOKEN_PREFIX + secrets.token_urlsafe(32)
        access_ttl = self._config.mcp_access_token_ttl_seconds
        self._conn.executemany(
            "INSERT INTO oauth_tokens (token_hash, kind, client_id, registration_id, connection_id, connected_at, "
            "expires_at, created_at, last_used_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [(_hash(access), "access", client_id, registration_id, connection_id, connected_at, now + access_ttl,
              now, now),
             (_hash(refresh), "refresh", client_id, registration_id, connection_id, connected_at,
              now + self._config.mcp_refresh_token_ttl_seconds, now, now)])
        self._evict_oldest_connections(registration_id, keep=connection_id)
        self._conn.execute("UPDATE oauth_clients SET last_used_at = ? WHERE client_id = ?", (now, client_id))
        return TokenResult(200, {"access_token": access, "token_type": "Bearer", "expires_in": access_ttl,
                                 "refresh_token": refresh, "scope": SCOPE})

    def _evict_oldest_connections(self, registration_id: str, keep: str) -> None:
        """At most MCP_MAX_CONNECTIONS_PER_REGISTRATION connections: the first connected go first."""
        rows = self._conn.execute(
            "SELECT connection_id FROM oauth_tokens WHERE registration_id = ? GROUP BY connection_id "
            "ORDER BY MIN(connected_at), connection_id", (registration_id,)).fetchall()
        excess = len(rows) - self._config.mcp_max_connections_per_registration
        for row in rows:
            if excess <= 0:
                break
            if row["connection_id"] != keep:
                self._conn.execute("DELETE FROM oauth_tokens WHERE registration_id = ? AND connection_id = ?",
                                   (registration_id, row["connection_id"]))
                excess -= 1

    # -------------------------------------------------------------- bearer (for /mcp)
    def registration_for_access_token(self, authorization: str | None) -> str | None:
        if not authorization or not authorization.startswith("Bearer "):
            return None
        token = authorization[len("Bearer "):]
        row = self._conn.execute(
            "SELECT t.registration_id FROM oauth_tokens AS t JOIN registrations AS r ON r.id = t.registration_id "
            "WHERE t.token_hash = ? AND t.kind = 'access' AND t.expires_at > ? AND r.revoked_at IS NULL",
            (_hash(token), int(self._clock()))).fetchone()
        return row["registration_id"] if row else None

    def touch_after_delivery(self, registration_id: str, authorization: str | None) -> None:
        """After a delivered discord_reply: last-used times for the client and the connection."""
        now = int(self._clock())
        if not authorization or not authorization.startswith("Bearer "):
            return
        token_hash = _hash(authorization[len("Bearer "):])
        with transaction(self._conn):
            # Only the client that made the call, so an issued client a member has
            # moved away from doesn't look recently used (and stays easy to delete).
            self._conn.execute(
                "UPDATE oauth_clients SET last_used_at = ? WHERE registration_id = ? AND client_id = "
                "(SELECT client_id FROM oauth_tokens WHERE token_hash = ?)", (now, registration_id, token_hash))
            # Every row of the connection, so the time survives the access
            # token's expiry and sweep.
            self._conn.execute(
                "UPDATE oauth_tokens SET last_used_at = ? WHERE registration_id = ? AND connection_id = "
                "(SELECT connection_id FROM oauth_tokens WHERE token_hash = ?)", (now, registration_id, token_hash))

    # -------------------------------------------------------------- helpers
    def _active_client(self, client_id: str) -> sqlite3.Row | None:
        return self._conn.execute(
            "SELECT c.* FROM oauth_clients c JOIN registrations r ON r.id = c.registration_id "
            "WHERE c.client_id = ? AND r.revoked_at IS NULL", (client_id,)).fetchone()

    def _authenticate_client(self, form: Mapping[str, str], authorization: str | None) -> str | None:
        """Issued clients: client_secret_post or client_secret_basic, never both.

        The public client: auth method "none" (a client_id and no secret), or,
        if OAUTH_PUBLIC_CLIENT_SECRET is set, that value by client_secret_post.
        Basic auth is never accepted for it.
        """
        basic = _parse_basic(authorization)
        posted_id, posted_secret = form.get("client_id"), form.get("client_secret")
        if basic and posted_secret:
            return None
        if self._is_public(posted_id) and not basic:
            expected = self._config.oauth_public_client_secret
            if expected:
                ok = bool(posted_secret) and hmac.compare_digest(_hash(posted_secret), _hash(expected))
            else:
                ok = not posted_secret
            return posted_id if ok else None
        client_id, secret = basic or (posted_id, posted_secret)
        if not client_id or not secret or self._is_public(client_id):
            return None
        client = self._active_client(client_id)
        if client is None or not hmac.compare_digest(client["secret_hash"], _hash(secret)):
            return None
        return client_id


def _parse_basic(authorization: str | None) -> tuple[str, str] | None:
    if not authorization or not authorization.startswith("Basic "):
        return None
    try:
        decoded = base64.b64decode(authorization[len("Basic "):], validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return None
    client_id, sep, secret = decoded.partition(":")
    return (unquote(client_id), unquote(secret)) if sep else None


def _with_query(uri: str, **params: str | None) -> str:
    query = urlencode({k: v for k, v in params.items() if v is not None})
    separator = "&" if urlsplit(uri).query else "?"
    return f"{uri}{separator}{query}"
