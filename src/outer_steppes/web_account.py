"""The account page (DESIGN.md, The account page).

GET  /account                  without a session: a static sign-in link (writes nothing)
GET  /account/login            starts the Discord login (HEAD is refused by the router)
POST /account/webhook          save: set or replace the member's automation
POST /account/webhook/remove   remove the automation; the member and connector stay
POST /account/leave            end the membership, as /leave does
POST /account/logout           end the session

Every POST needs the session cookie, the session's CSRF token, an Origin equal
to PUBLIC_BASE_URL's, and a form body of at most 8 KB, and acts only for the
Discord user the session belongs to. Every POST except sign-out (which only
ends the session) also needs that user to still be a member. A session never
creates a member: joining happens only at login. The stored URL and
secret are never shown back, and form bodies are never logged.
"""

from __future__ import annotations

import logging
import sqlite3
import time
from collections.abc import Callable, Collection
from datetime import datetime, timezone
from urllib.parse import urlencode, urlsplit

from aiohttp import web

from . import foreign, metrics, registry, sessions, web_pages
from .config import PERSONAL, SHARED, Config
from .db import transaction
from .oauth import ACCOUNT_PATH, connector_settings
from .policy import is_paused
from .url_policy import Resolver, UrlRejected, system_resolver, validate_automation_url
from .web_pages import REFERRER_ACCOUNT, esc

log = logging.getLogger(__name__)

LOGIN_PATH = ACCOUNT_PATH + "/login"
SAVE_PATH = ACCOUNT_PATH + "/webhook"
REMOVE_PATH = ACCOUNT_PATH + "/webhook/remove"
LEAVE_PATH = ACCOUNT_PATH + "/leave"
LOGOUT_PATH = ACCOUNT_PATH + "/logout"
MAX_FORM_BYTES = 8 * 1024
TITLE = "Outer Steppes"

MSG_UNAVAILABLE = "The account page isn't available on this bridge, because Discord login is off. Ask the operator."
MSG_SIGN_IN = "Sign in with Discord to set your grok.com automation, remove it, or leave Outer Steppes."
MSG_SESSION_GONE = "Your sign-in expired or ended. Open the account page again to sign in with Discord."
MSG_FORBIDDEN = "That form was not accepted. Reload the page and try again."
MSG_TOO_LARGE = "That form was too large."
MSG_PAUSED = "The bridge is paused, so automations can't be saved right now. Try again later."
MSG_NOT_ALLOWED = "This bridge doesn't allow your Discord account to set an automation."
MSG_OPERATOR_ONLY = "On this bridge, only the operator sets automations. Ask the operator."
# Word for word from DESIGN.md, Operator warning (a test checks it). Shown directly above the save form.
MSG_OPERATOR_TRUST = ("Only save an automation here if you trust whoever runs this bridge. Once it's saved, they can "
                      "send your automation any prompt, and each run can use your grok.com connectors. If you don't "
                      "trust them, run your own bridge instead.")
MSG_RATE_LIMITED = "Too many saves in the last hour. Try again later."
MSG_URL_REJECTED = ("That trigger URL was not accepted. It must be the webhook URL exactly as shown: "
                    "https://grok.com/webhook/automation/… for a grok.com automation, or "
                    "https://api2.cursor.sh/automations/webhook/… for a Grok Bot routine.")
MSG_SECRET_REJECTED = ("That secret was not accepted. It must be the whsec_… secret grok.com shows, or the crsr_… "
                       "key Grok Bot shows, matching the URL.")
# The /leave text without its bold markers: this page renders no markdown (DESIGN.md, Member lifecycle).
MSG_LEFT = registry.MSG_LEFT.replace("**", "")

# How to join again, by MEMBERSHIP (DESIGN.md, The account page). Only a shared bridge takes members who sign in.
REJOIN = {SHARED: "You can join again later by connecting grok.com or signing in here.",
          PERSONAL: "Joining again needs a new claim link or the operator."}
MSG_REJOIN_OPERATOR = "Joining again needs the operator."

# Only these fixed notices are shown; the query string selects one and is never echoed.
NOTICES = {
    "saved": "Saved. Your automation is set.",
    "removed": "Automation removed. You're still a member, and this doesn't disconnect grok.com's connector.",
    "left": MSG_LEFT,
    "signed_out": "Signed out.",
}

# Word for word from DESIGN.md, Automation instructions > MCP delivery (a test checks it).
INSTRUCTIONS = "Follow the instructions field in the trigger body exactly."

CONNECTOR_ROWS = (("Name", "connector_name"), ("MCP server URL", "mcp_server_url"), ("Client ID", "client_id"),
                  ("Client secret", "client_secret"), ("Authorization endpoint", "authorization_endpoint"),
                  ("Token endpoint", "token_endpoint"), ("Scopes", "scopes"),
                  ("Token auth method", "token_auth_method"))


def origin_of(url: str) -> str:
    """The serialized origin a browser sends for pages under `url`: lowercase host, default port dropped."""
    parts = urlsplit(url)
    port = f":{parts.port}" if parts.port not in (None, 443) else ""
    return f"{parts.scheme}://{parts.hostname}{port}"


class AccountPages:
    def __init__(self, conn: sqlite3.Connection, config: Config,
                 wake_finalizer: Callable[[], None] = lambda: None,
                 resolver: Resolver = system_resolver, bind_hosts: Collection[str] = (),
                 clock: Callable[[], float] = time.time) -> None:
        self._conn = conn
        self._config = config
        self._wake = wake_finalizer
        self._resolver = resolver
        self._bind_hosts = frozenset(bind_hosts)
        self._clock = clock
        self._origin = origin_of(config.public_base_url)

    @property
    def available(self) -> bool:
        return self._config.discord_login_enabled

    def unavailable(self) -> web.Response:
        return self._message(404, MSG_UNAVAILABLE)

    # -------------------------------------------------------------- helpers
    def _now(self) -> int:
        return int(self._clock())

    def _url(self, path: str) -> str:
        return self._config.public_base_url + path

    def _page(self, status: int, body_html: str) -> web.Response:
        # form-action 'self' only: no account form redirects off the bridge.
        return web_pages.render_html(status, TITLE, body_html, referrer_policy=REFERRER_ACCOUNT)

    def _message(self, status: int, message: str) -> web.Response:
        return self._page(status, f"<p>{esc(message)}</p><p><a href='{esc(self._url(ACCOUNT_PATH))}'>"
                                  "Back to the account page</a></p>")

    def _redirect(self, done: str, clear_cookie: bool = False) -> web.Response:
        response = web.Response(status=303, headers={
            "Location": self._url(ACCOUNT_PATH) + "?" + urlencode({"done": done}),
            **web_pages.page_headers(referrer_policy=REFERRER_ACCOUNT)})
        if clear_cookie:
            clear_session_cookie(response)
        return response

    def _session(self, request: web.Request) -> sessions.Session | None:
        return sessions.load(self._conn, request.cookies.get(sessions.SESSION_COOKIE), self._now())

    async def _authorized_post(self, request: web.Request) -> tuple[sessions.Session | None, dict, web.Response | None]:
        """The checks every POST shares. Nothing is written when one fails."""
        if not self.available:
            return None, {}, self._message(404, MSG_UNAVAILABLE)
        session = self._session(request)
        if session is None:
            return None, {}, self._message(403, MSG_SESSION_GONE)
        if request.headers.get("Origin") != self._origin:
            return None, {}, self._message(403, MSG_FORBIDDEN)
        # No Content-Length (a chunked body): refuse rather than read a body of unknown size.
        # Browsers always send one for form posts.
        if request.content_length is None or request.content_length > MAX_FORM_BYTES:
            return None, {}, self._message(413, MSG_TOO_LARGE)
        if request.content_type != "application/x-www-form-urlencoded":
            return None, {}, self._message(400, MSG_FORBIDDEN)
        form = await request.post()
        if any(len(form.getall(key)) > 1 for key in form):
            return None, {}, self._message(400, MSG_FORBIDDEN)
        fields = {key: str(value) for key, value in form.items()}
        if not session.csrf_matches(fields.get("csrf")):
            return None, {}, self._message(403, MSG_FORBIDDEN)
        return session, fields, None

    @staticmethod
    def _result(name: str) -> None:
        metrics.METRICS.membership_changes.inc("self", name)
        log.info("account page: %s", name)  # never the URL, the secret, or the form

    # -------------------------------------------------------------- GET /account
    async def show(self, request: web.Request) -> web.Response:
        if not self.available:
            return self._message(404, MSG_UNAVAILABLE)
        notice = NOTICES.get(request.query.get("done", ""), "")
        session = self._session(request)
        member = registry.active_registration(self._conn, session.discord_user_id) if session else None
        if session is None or member is None:
            # Static: a link preview or prefetch of /account starts nothing.
            parts = [f"<p><b>{esc(notice)}</b></p>" if notice else "", f"<p>{esc(MSG_SIGN_IN)}</p>",
                     f"<p><a href='{esc(self._url(LOGIN_PATH))}'>Sign in with Discord</a></p>"]
            return self._page(200, "".join(parts))
        return self._render(session, member, 200, notice=notice)

    def _render(self, session: sessions.Session, member: sqlite3.Row, status: int, notice: str = "",
                error: str = "") -> web.Response:
        csrf = esc(session.csrf)
        automation = registry.active_automation(self._conn, member["id"])

        def form(path: str, button: str) -> str:
            return (f"<form method=post action='{esc(self._url(path))}'><input type=hidden name=csrf value='{csrf}'>"
                    f"<button>{esc(button)}</button></form>")

        parts = [f"<p>Signed in as <b>{esc(session.username or session.discord_user_id)}</b>.</p>"]
        if notice:
            parts.append(f"<p><b>{esc(notice)}</b></p>")
        parts.append("<h2>Automation</h2>")
        if automation is None:
            parts.append("<p>Not set.</p>")
        else:
            when = datetime.fromtimestamp(automation["created_at"], timezone.utc).strftime("%Y-%m-%d")
            parts.append(f"<p>Set on {when}. The trigger URL and secret are never shown again; saving replaces "
                         "them.</p>")
        if error:
            parts.append(f"<p class=error>{esc(error)}</p>")
        if self._config.members_set_automations:
            parts.append(f"<p>{esc(MSG_OPERATOR_TRUST)}</p>")
            parts.append(
                f"<form method=post action='{esc(self._url(SAVE_PATH))}'>"
                f"<input type=hidden name=csrf value='{csrf}'>"
                "<label>Trigger URL<input type=text name=automation_url autocomplete=off required "
                "placeholder='https://grok.com/webhook/automation/… or https://api2.cursor.sh/automations/webhook/…'></label>"
                "<label>Secret or key<input type=password name=signing_secret autocomplete=off required "
                "placeholder='whsec_… or crsr_…'></label><button>Save</button></form>")
        else:
            parts.append(f"<p>{esc(MSG_OPERATOR_ONLY)}</p>")
        if automation is not None:
            parts.append(form(REMOVE_PATH, "Remove automation"))
        parts.append(self._connector(member))
        parts.append(self._next_steps())
        parts.append("<h2>Leave Outer Steppes</h2><p>Leaving deletes your automation and every grok.com connector "
                     "connection. Pending answers won't be posted. "
                     f"{esc(REJOIN.get(self._config.membership, MSG_REJOIN_OPERATOR))}</p>"
                     + form(LEAVE_PATH, "Leave Outer Steppes"))
        parts.append(form(LOGOUT_PATH, "Sign out"))
        return self._page(status, "".join(parts))

    def _connector(self, member: sqlite3.Row) -> str:
        values = connector_settings(self._config)
        # Only the warning, and only with an automation, whose answers need the connector; no "connected" claim
        # (DESIGN.md, Connector status)
        automation = registry.active_automation(self._conn, member["id"])
        disconnected = (automation is not None
                        and registry.connector_state(self._conn, member, self._now()) == "disconnected")
        status = ""
        if disconnected:
            status = f"<p class=error>{esc(registry.MSG_CONNECTOR_DOWN.replace('**', ''))}.</p>"
        elif automation is not None and foreign.automation_banned(self._conn, automation["id"]):
            note = foreign.text(foreign.MSG_BLOCKED_NOTE, self._config.membership)
            status = f"<p class=error>{esc(note.replace('**', ''))}</p>"
        # The connector's member's warning shows whatever else is true (DESIGN.md, Connector status).
        if foreign.warned(self._conn, member["id"], self._now()):
            status += f"<p class=error>{esc(foreign.MSG_CONNECTOR_USED.replace('**', ''))}</p>"
        rows = "".join(f"<tr><td>{esc(label)}</td><td><code>{esc(values[key] or '(leave empty)')}</code></td></tr>"
                       for label, key in CONNECTOR_ROWS)
        return ("<h2>Connector</h2><p>At grok.com/connectors, add a custom connector with these values. None of "
                f"them is a secret.</p><table>{rows}</table>{status}")

    def _next_steps(self) -> str:
        install = "https://discord.com/oauth2/authorize?" + urlencode({
            "client_id": self._config.discord_application_id, "integration_type": "1",
            "scope": "applications.commands"})
        return ("<h2>Next steps</h2><ol>"
                "<li>In grok.com, create an automation with a webhook trigger, and save its URL and secret above. "
                f"Check that this page's address is <code>{esc(self._origin)}</code> before pasting a secret.</li>"
                f"<li>Set the automation's instructions to:<pre>{esc(INSTRUCTIONS)}</pre></li>"
                f"<li>Add the Discord app to your account: <a href='{esc(install)}'>install link</a>.</li>"
                "<li>In Discord, run <code>/webhook status</code>, then <code>/ask</code>.</li></ol>")

    # -------------------------------------------------------------- POST /account/webhook
    async def save(self, request: web.Request) -> web.Response:
        session, form, refused = await self._authorized_post(request)
        if refused is not None:
            return refused
        user_id = session.discord_user_id
        if not self._config.members_set_automations:
            return self._message(403, MSG_OPERATOR_ONLY)
        if self._config.user_allowlist and user_id not in self._config.user_allowlist:
            return self._message(403, MSG_NOT_ALLOWED)
        if is_paused(self._conn):
            return self._message(503, MSG_PAUSED)
        member = registry.active_registration(self._conn, user_id)
        if member is None:
            return self._message(403, MSG_SESSION_GONE)

        url, secret = form.get("automation_url", "").strip(), form.get("signing_secret", "").strip()
        try:
            registry.validate_secret(secret, registry.kind_for_url(url))
        except registry.SecretRejected:
            self._result("secret_rejected")
            return self._render(session, member, 400, error=MSG_SECRET_REJECTED)
        # A read-only lookup before any DNS; checked again inside the save transaction (DESIGN.md, The account page).
        if foreign.is_banned(self._conn, foreign.secret_hash(secret)):
            return self._banned(session, member)
        try:
            # DNS runs here, outside any transaction.
            accepted = await validate_automation_url(url, self._bind_hosts, self._resolver)
        except UrlRejected as exc:
            log.info("account page: automation URL %s", exc)  # the reason only, never the URL
            self._result("url_rejected")
            return self._render(session, member, 400, error=MSG_URL_REJECTED)

        now = self._now()
        try:
            with transaction(self._conn):
                # Re-checked inside the write: pause or leaving may have happened while DNS ran.
                if is_paused(self._conn):
                    return self._message(503, MSG_PAUSED)
                member = registry.active_registration(self._conn, user_id)
                if member is None:
                    return self._message(403, MSG_SESSION_GONE)
                # Counted inside the write transaction, so concurrent saves can't exceed the limit.
                # Only accepted saves count: a refused URL or secret wrote nothing (DESIGN.md, Limits).
                if sessions.changes_in_window(self._conn, user_id, now) >= sessions.MAX_CHANGES_PER_WINDOW:
                    self._result("rate_limited")
                    return self._message(429, MSG_RATE_LIMITED)
                # Store the canonical form: exactly what was validated is what the worker POSTs to.
                replaced, withdrawn = registry.set_automation_in_tx(self._conn, member["id"], accepted.url, secret,
                                                                    now)
                sessions.record_change_in_tx(self._conn, user_id, now)
                registry.touch_in_tx(self._conn, member["id"], now)
        except registry.SecretBanned:  # a ban that landed since the first lookup; the transaction rolled back
            return self._banned(session, member)
        except registry.UrlInUse as exc:
            # Both membership ids, never the URL, so the operator can find the holder (DESIGN.md, Members and
            # automations).
            log.warning("account page: trigger URL in use (holder %s, saver %s)", exc.holder, exc.saver)
            self._result("url_in_use")
            return self._render(session, member, 400, error=foreign.MSG_URL_IN_USE)
        self._result("webhook_replaced" if replaced else "webhook_set")
        if withdrawn:
            self._wake()
        return self._redirect("saved")

    def _banned(self, session: sessions.Session, member: sqlite3.Row) -> web.Response:
        log.info("account page: signing secret banned")
        self._result("secret_banned")
        return self._render(session, member, 400, error=foreign.MSG_SECRET_BANNED)

    # -------------------------------------------------------------- POST /account/webhook/remove
    async def remove(self, request: web.Request) -> web.Response:
        """Works while paused and with MEMBERSHIP=operator, and is never rate-limited."""
        session, _, refused = await self._authorized_post(request)
        if refused is not None:
            return refused
        with transaction(self._conn):
            member = registry.active_registration(self._conn, session.discord_user_id)
            if member is None:
                return self._message(403, MSG_SESSION_GONE)
            removed, withdrawn = registry.remove_automation_in_tx(self._conn, member["id"], self._now())
        if removed:
            self._result("webhook_removed")
        if withdrawn:
            self._wake()
        return self._redirect("removed")

    # -------------------------------------------------------------- POST /account/leave
    async def leave(self, request: web.Request) -> web.Response:
        """The same as /leave confirm:True, and works while paused. revoke_in_tx also ends every session of the user."""
        session, _, refused = await self._authorized_post(request)
        if refused is not None:
            return refused
        with transaction(self._conn):
            member = registry.active_registration(self._conn, session.discord_user_id)
            if member is None:
                return self._message(403, MSG_SESSION_GONE)
            withdrawn = registry.revoke_in_tx(self._conn, member["id"], self._now())
        self._result("left")
        if withdrawn:
            self._wake()
        return self._redirect("left", clear_cookie=True)

    # -------------------------------------------------------------- POST /account/logout
    async def logout(self, request: web.Request) -> web.Response:
        session, _, refused = await self._authorized_post(request)
        if refused is not None:
            return refused
        with transaction(self._conn):
            sessions.delete_in_tx(self._conn, session)
        return self._redirect("signed_out", clear_cookie=True)


def set_session_cookie(response: web.StreamResponse, session_id: str) -> None:
    # __Host- requires Secure and Path=/ and forbids Domain; browsers reject it otherwise.
    response.set_cookie(sessions.SESSION_COOKIE, session_id, max_age=sessions.SESSION_TTL_SECONDS, path="/",
                        secure=True, httponly=True, samesite="Lax")


def clear_session_cookie(response: web.StreamResponse) -> None:
    response.del_cookie(sessions.SESSION_COOKIE, path="/", secure=True, httponly=True, samesite="Lax")
