"""HTTP routes, split across two listeners (DESIGN.md, Architecture).

The public app holds only public routes: Funnel forwards every route on it. The admin app
(loopback_api.py) is bound to loopback and carries /v1; /metrics and /readyz
join it in PR 6.
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable

from aiohttp import web

from . import metrics, web_account, web_pages
from .config import Config
from .interactions import AckResult, InteractionHandler
from .mcp_server import McpServer
from .claim import CLAIM_PATH
from .oauth import (ACCOUNT_PATH, CONNECT_WARNING, DISCORD_CALLBACK_PATH, LOGIN_COOKIE, MSG_BAD_STATE,
                    MSG_CLAIM_LINK_INVALID, MSG_CONNECT_NEXT,
                    PENDING_TTL_SECONDS, AuthorizeResult, Identifier, OAuthServer)
from .signatures import SIGNATURE_HEADER, TIMESTAMP_HEADER, SignatureVerifier
from .web_account import AccountPages

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 256 * 1024

INTERACTION_PING = 1
RESPONSE_PONG = 1

VERIFIER_KEY = web.AppKey("verifier", SignatureVerifier)
HANDLER_KEY = web.AppKey("handler", InteractionHandler)
SPAWN_KEY = web.AppKey("spawn", Callable)
WAKE_FINALIZER_KEY = web.AppKey("wake_finalizer", Callable)
OAUTH_KEY = web.AppKey("oauth", OAuthServer)
MCP_KEY = web.AppKey("mcp", McpServer)
DISCORD_LOGIN_KEY = web.AppKey("discord_login", Identifier)
ACCOUNT_KEY = web.AppKey("account", AccountPages)
NO_STORE = {"Cache-Control": "no-store", "Pragma": "no-cache"}


async def healthz(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


# The `command` metric label. One entry per command scripts/osctl_command.py registers
# (tests/test_ops.py checks they match); any other name counts as "other".
KNOWN_COMMANDS = ("ask", "status", "settings", "webhook", "leave")


async def interactions(request: web.Request) -> web.StreamResponse:
    started = time.monotonic()
    command, result = "other", "error"
    try:
        response, command, result = await _interactions(request)
        return response
    finally:
        metrics.METRICS.interactions.inc(command, result)
        metrics.METRICS.ack_latency.observe(time.monotonic() - started)


async def _interactions(request: web.Request) -> tuple[web.StreamResponse, str, str]:
    """Returns the response plus the metric labels: a known command name and a fixed result."""
    body = await _read_capped(request)
    if body is None:
        return web.Response(status=413), "other", "too_large"

    if not request.app[VERIFIER_KEY].verify(
        request.headers.get(SIGNATURE_HEADER),
        request.headers.get(TIMESTAMP_HEADER),
        body,
    ):
        return web.Response(status=401, text="invalid request signature"), "other", "bad_signature"

    try:
        interaction = json.loads(body)
    except ValueError:
        interaction = None
    if not isinstance(interaction, dict) or not isinstance(interaction.get("type"), int) \
            or isinstance(interaction.get("type"), bool):
        log.warning("signed interaction was not a JSON object with an integer type")
        return web.Response(status=400), "other", "bad_request"

    if interaction["type"] == INTERACTION_PING:
        return web.json_response({"type": RESPONSE_PONG}), "ping", "pong"

    name = (interaction.get("data") or {}).get("name") if isinstance(interaction.get("data"), dict) else None
    command = name if name in KNOWN_COMMANDS else "other"

    result: AckResult = await request.app[HANDLER_KEY].handle(interaction)
    response = web.json_response(result.body)
    outcome = "deferred" if result.body.get("type") == 5 else "ephemeral"
    if result.spawn is None and not result.wake_finalizer:
        return response, command, outcome

    # Step 13: write the whole response first. Only then start the worker, or
    # wake the finalizer to send withdrawal messages after /leave.
    await response.prepare(request)
    await response.write_eof()
    if result.spawn is not None:
        request.app[SPAWN_KEY](result.spawn.interaction_id, result.spawn.prompt)
    if result.wake_finalizer:
        request.app[WAKE_FINALIZER_KEY]()
    return response, command, outcome


PAGE_TITLE = "Outer Steppes"


def _authorize_response(result: AuthorizeResult, clear_login_cookie: bool = False,
                        base_url: str = "") -> web.Response:
    if result.continue_url is not None:
        response = _connect_warning_page(result)
    elif result.location is None and result.link_to_account:
        link = base_url + ACCOUNT_PATH
        response = web_pages.render_html(
            result.status, PAGE_TITLE, f"<p>{web_pages.esc(result.page or '')}</p>"
            f"<p><a href='{web_pages.esc(link)}'>Back to the account page</a></p>")
    elif result.location is None:
        response = web_pages.render(result.status, PAGE_TITLE, result.page or "")
    else:
        response = web.Response(status=result.status, headers={"Location": result.location,
                                                               **web_pages.page_headers()})
    if result.login_cookie is not None:
        # Binds the Discord round trip to this browser (DESIGN.md, Pending logins). __Host- requires Secure
        # and Path=/ and forbids Domain; browsers reject it otherwise.
        response.set_cookie(LOGIN_COOKIE, result.login_cookie, max_age=PENDING_TTL_SECONDS,
                            path="/", secure=True, httponly=True, samesite="Lax")
    elif clear_login_cookie:
        response.del_cookie(LOGIN_COOKIE, path="/", secure=True, httponly=True, samesite="Lax")
    if result.session_cookie is not None:
        web_account.set_session_cookie(response, result.session_cookie)
    return response


def _connect_warning_page(result: AuthorizeResult) -> web.Response:
    """The connect-time warning (DESIGN.md, Connect-time warning): Cancel first, as the main choice.

    Both URLs are already URL-encoded; escaping them here makes them safe inside the attribute.
    """
    paragraphs = "".join(f"<p>{web_pages.esc(text)}</p>" for text in (*CONNECT_WARNING, MSG_CONNECT_NEXT))
    return web_pages.render_html(
        result.status, PAGE_TITLE,
        f"{paragraphs}<p class=choice><a class=main href='{web_pages.esc(result.cancel_url or '')}'>Cancel</a>"
        f"<a href='{web_pages.esc(result.continue_url or '')}'>Continue</a></p>")


async def oauth_authorize(request: web.Request) -> web.Response:
    """Browser step of the grok.com connector's OAuth flow."""
    if any(len(request.query.getall(key)) > 1 for key in request.query):
        return web_pages.render(400, PAGE_TITLE, "Repeated OAuth parameters are not allowed.")
    return _authorize_response(request.app[OAUTH_KEY].authorize(dict(request.query)))


async def discord_callback(request: web.Request) -> web.Response:
    """Discord sends the browser back here after the member approves (or cancels)."""
    identifier = request.app.get(DISCORD_LOGIN_KEY)
    if identifier is None or any(len(request.query.getall(key)) > 1 for key in request.query):
        return web_pages.render(400, PAGE_TITLE, MSG_BAD_STATE)
    result = await request.app[OAUTH_KEY].discord_callback(dict(request.query), request.cookies.get(LOGIN_COOKIE),
                                                           identifier)
    return _authorize_response(result, clear_login_cookie=True,
                               base_url=request.app[OAUTH_KEY].public_base_url)


# ------------------------------------------------------------------ the account page (PR 8c)

async def account_page(request: web.Request) -> web.Response:
    return await _account(request).show(request)


async def account_login(request: web.Request) -> web.Response:
    """The only account route that starts a Discord login. GET only: HEAD gets 405."""
    pages = _account(request)
    if not pages.available or request.app.get(DISCORD_LOGIN_KEY) is None:
        return pages.unavailable()
    return _authorize_response(request.app[OAUTH_KEY].start_account_login())


async def account_claim(request: web.Request) -> web.Response:
    """The claim link (DESIGN.md, Claiming the bridge). GET only: HEAD gets 405, so a preview starts no login."""
    pages = _account(request)
    if not pages.available or request.app.get(DISCORD_LOGIN_KEY) is None:
        return pages.unavailable()
    if any(len(request.query.getall(key)) > 1 for key in request.query):
        return web_pages.render(400, PAGE_TITLE, MSG_CLAIM_LINK_INVALID)
    # The redirect to Discord carries Referrer-Policy: no-referrer, so the token never leaves the bridge.
    return _authorize_response(request.app[OAUTH_KEY].start_claim_login(request.query.get("token", "")))


def _account_post(method_name: str):
    async def handler(request: web.Request) -> web.Response:
        return await getattr(_account(request), method_name)(request)
    return handler


def _account(request: web.Request) -> AccountPages:
    return request.app[ACCOUNT_KEY]


async def oauth_token(request: web.Request) -> web.Response:
    """Server-to-server step: code (with PKCE) or refresh token for an access token."""
    if request.content_type != "application/x-www-form-urlencoded":
        return web.json_response({"error": "invalid_request"}, status=400, headers=NO_STORE)
    form = await request.post()
    if any(len(form.getall(key)) > 1 for key in form):
        return web.json_response({"error": "invalid_request"}, status=400, headers=NO_STORE)
    result = request.app[OAUTH_KEY].token({k: str(v) for k, v in form.items()},
                                          request.headers.get("Authorization"))
    headers = dict(NO_STORE)
    if result.www_authenticate:
        headers["WWW-Authenticate"] = result.www_authenticate
    return web.json_response(result.body, status=result.status, headers=headers)


async def mcp_post(request: web.Request) -> web.Response:
    return await request.app[MCP_KEY].handle(request)


async def mcp_not_allowed(request: web.Request) -> web.Response:
    # No server-initiated SSE stream and no sessions (DESIGN.md, MCP server).
    return web.Response(status=405, headers={"Allow": "POST"})


async def _read_capped(request: web.Request, limit: int = MAX_BODY_BYTES) -> bytes | None:
    """Read the raw body, or return None once it exceeds `limit`."""
    if request.content_length is not None and request.content_length > limit:
        return None
    chunks: list[bytes] = []
    total = 0
    async for chunk in request.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


def create_public_app(config: Config, handler: InteractionHandler,
                      spawn: Callable[[str, str], None],
                      wake_finalizer: Callable[[], None] = lambda: None,
                      clock: Callable[[], float] = time.time,
                      oauth: OAuthServer | None = None,
                      mcp: McpServer | None = None,
                      discord_login: Identifier | None = None,
                      account: AccountPages | None = None) -> web.Application:
    app = web.Application()
    app[VERIFIER_KEY] = SignatureVerifier(config.discord_public_key, config.signature_max_skew_seconds, clock)
    app[HANDLER_KEY] = handler
    app[SPAWN_KEY] = spawn
    app[WAKE_FINALIZER_KEY] = wake_finalizer
    app.router.add_get("/healthz", healthz)
    app.router.add_post("/interactions", interactions)
    if oauth is not None and mcp is not None:
        app[OAUTH_KEY] = oauth
        app[MCP_KEY] = mcp
        if discord_login is not None:
            app[DISCORD_LOGIN_KEY] = discord_login
        app.router.add_get("/oauth/authorize", oauth_authorize)
        # Always routed, so the route table matches DESIGN.md's; without Discord login
        # there are no pending logins, and every callback gets the bad-state page.
        app.router.add_get(DISCORD_CALLBACK_PATH, discord_callback)
        # The account page: also always routed. Without Discord login it shows a
        # "not available" page.
        app[ACCOUNT_KEY] = account or AccountPages(oauth.conn, config, wake_finalizer, clock=clock)
        app.router.add_get(ACCOUNT_PATH, account_page)
        # No automatic HEAD: a HEAD (a link preview, say) must not start a Discord login.
        app.router.add_get(web_account.LOGIN_PATH, account_login, allow_head=False)
        app.router.add_get(CLAIM_PATH, account_claim, allow_head=False)
        app.router.add_post(web_account.SAVE_PATH, _account_post("save"))
        app.router.add_post(web_account.REMOVE_PATH, _account_post("remove"))
        app.router.add_post(web_account.LEAVE_PATH, _account_post("leave"))
        app.router.add_post(web_account.LOGOUT_PATH, _account_post("logout"))
        app.router.add_post("/oauth/token", oauth_token)
        app.router.add_post("/mcp", mcp_post)
        app.router.add_get("/mcp", mcp_not_allowed)
        app.router.add_delete("/mcp", mcp_not_allowed)
    return app
