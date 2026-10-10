"""The admin listener's /v1 API (DESIGN.md, Registration (operator)).

Bound to loopback only, never proxied, and every /v1 request needs the
registration bearer. Responses never contain an automation URL or a signing
secret (the one-time client secret and claim link aside), and errors never
echo input. While paused, POSTs that create or change registrations, or mint a
claim link, return 403; every DELETE and POST /v1/pause still work.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import logging
import sqlite3
import time
from collections.abc import Awaitable, Callable, Collection

from aiohttp import web

from . import claim, db, foreign, metrics, oauth, registry
from .config import Config
from .db import transaction
from .policy import is_paused
from .url_policy import Resolver, UrlRejected, system_resolver, validate_automation_url

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 64 * 1024
MAX_LABEL_CHARS = 100

CONFIG_KEY = web.AppKey("config", Config)
CONN_KEY = web.AppKey("conn", sqlite3.Connection)
WAKE_KEY = web.AppKey("wake_finalizer", Callable)
CLOCK_KEY = web.AppKey("clock", Callable)
RESOLVER_KEY = web.AppKey("resolver", Callable)
BIND_HOSTS_KEY = web.AppKey("bind_hosts", frozenset)
BACKGROUND_KEY = web.AppKey("background_alive", Callable)


def presented_token(auth_headers: list[str], query_string: str) -> str | None:
    """DESIGN.md, Loopback bearer: exactly one `Bearer <token>`, no query string."""
    if query_string or len(auth_headers) != 1:
        return None
    value = auth_headers[0]
    if not value.startswith("Bearer "):
        return None
    token = value[len("Bearer "):]
    if not token or any(ch.isspace() for ch in token):
        return None
    return token


def bearer_matches(presented: str, expected: str) -> bool:
    return hmac.compare_digest(hashlib.sha256(presented.encode()).digest(),
                               hashlib.sha256(expected.encode()).digest())


@web.middleware
async def require_bearer(request: web.Request, handler: Callable[[web.Request], Awaitable[web.StreamResponse]]):
    if request.path.startswith("/v1/") or request.path == "/v1":
        token = presented_token(request.headers.getall("Authorization", []), request.query_string)
        if token is None or not bearer_matches(token, request.app[CONFIG_KEY].registration_token):
            return web.Response(status=401)
    return await handler(request)


def _error(status: int, code: str) -> web.Response:
    return web.json_response({"error": code}, status=status)


async def _json_body(request: web.Request) -> dict | None:
    if request.content_length is not None and request.content_length > MAX_BODY_BYTES:
        return None
    raw = await request.content.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        return None
    try:
        body = json.loads(raw)
    except ValueError:
        return None
    return body if isinstance(body, dict) else None


def _is_snowflake(value: object) -> bool:
    return isinstance(value, str) and value.isascii() and value.isdigit() and len(value) <= 20


def _now(request: web.Request) -> int:
    return int(request.app[CLOCK_KEY]())


# ------------------------------------------------------------------ registrations

async def upsert_registration(request: web.Request) -> web.Response:
    conn = request.app[CONN_KEY]
    if is_paused(conn):
        return web.Response(status=403)
    body = await _json_body(request)
    if body is None:
        return _error(400, "bad_request")

    user_id, url, secret, label = (body.get(k) for k in
                                   ("discord_user_id", "automation_url", "signing_secret", "label"))
    if not _is_snowflake(user_id):
        return _error(400, "bad_discord_user_id")
    if label is not None and (not isinstance(label, str) or len(label) > MAX_LABEL_CHARS):
        return _error(400, "bad_label")
    try:
        registry.validate_secret(secret, registry.kind_for_url(url))
    except registry.SecretRejected:
        metrics.METRICS.membership_changes.inc("operator", "secret_rejected")
        return _error(400, "secret_rejected")
    # A read-only lookup before any DNS; checked again inside the save transaction (DESIGN.md, Another member's
    # connector).
    if foreign.is_banned(conn, foreign.secret_hash(secret)):
        return _secret_banned()
    try:
        # DNS runs here, outside any transaction.
        accepted = await validate_automation_url(url, request.app[BIND_HOSTS_KEY], request.app[RESOLVER_KEY])
    except UrlRejected as exc:
        log.info("registration rejected: automation URL %s", exc)  # reason only, never the URL
        metrics.METRICS.membership_changes.inc("operator", "url_rejected")
        return _error(400, "url_rejected")

    try:
        with transaction(conn):
            if is_paused(conn):  # paused while DNS ran
                return web.Response(status=403)
            # Store the canonical form: exactly what was validated is what the worker POSTs to.
            result = registry.upsert_registration_in_tx(conn, user_id, accepted.url, secret, label, _now(request))
    except registry.SecretBanned:  # the transaction rolled back whole, so no member was created
        return _secret_banned()
    except registry.UrlInUse as exc:
        # Both membership ids (`new` for a member the save would have created), never the URL or a Discord id.
        log.warning("registration rejected: trigger URL in use (holder %s, saver %s)", exc.holder, exc.saver)
        metrics.METRICS.membership_changes.inc("operator", "url_in_use")
        return _error(400, "url_in_use")
    if result["created"]:
        metrics.METRICS.membership_changes.inc("operator", "joined")
    metrics.METRICS.membership_changes.inc("operator", "webhook_replaced" if result["replaced"] else "webhook_set")
    if result["withdrawn"]:
        request.app[WAKE_KEY]()
    return web.json_response(result)


def _secret_banned() -> web.Response:
    log.info("registration rejected: signing secret banned")
    metrics.METRICS.membership_changes.inc("operator", "secret_banned")
    return _error(400, "secret_banned")


async def list_members(request: web.Request) -> web.Response:
    return web.json_response(registry.list_members(request.app[CONN_KEY]))


async def revoke_registration(request: web.Request) -> web.Response:
    conn = request.app[CONN_KEY]
    user_id = request.match_info["discord_user_id"]
    with transaction(conn):
        registration = registry.active_registration(conn, user_id)
        if registration is None:
            return _error(404, "not_found")
        withdrawn = registry.revoke_in_tx(conn, registration["id"], _now(request))
    metrics.METRICS.membership_changes.inc("operator", "left")
    if withdrawn:
        request.app[WAKE_KEY]()
    return web.json_response({"revoked": True, "withdrawn": withdrawn})


async def remove_automation(request: web.Request) -> web.Response:
    """Remove only the member's automation. The member and their connector stay.

    Idempotent, and like every DELETE it works while paused.
    """
    conn = request.app[CONN_KEY]
    with transaction(conn):
        registration = registry.active_registration(conn, request.match_info["discord_user_id"])
        if registration is None:
            return _error(404, "not_found")
        removed, withdrawn = registry.remove_automation_in_tx(conn, registration["id"], _now(request))
    if removed:
        metrics.METRICS.membership_changes.inc("operator", "webhook_removed")
    if withdrawn:
        request.app[WAKE_KEY]()
    return web.json_response({"removed": removed, "withdrawn": withdrawn})


# ------------------------------------------------------------------ MCP connector OAuth clients

async def issue_mcp_client(request: web.Request) -> web.Response:
    """Everything the owner types into grok.com's custom-connector form. The secret is shown once."""
    conn = request.app[CONN_KEY]
    with transaction(conn):
        if is_paused(conn):
            return web.Response(status=403)
        registration = registry.active_registration(conn, request.match_info["discord_user_id"])
        if registration is None:
            return _error(404, "not_found")
        client_id, client_secret = oauth.issue_client_in_tx(conn, registration["id"], _now(request))
    base = request.app[CONFIG_KEY].public_base_url
    return web.json_response({
        "client_id": client_id,
        "client_secret": client_secret,
        "mcp_server_url": base + "/mcp",
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "scopes": oauth.SCOPE,
        "token_auth_method": "client_secret_post",
    })


async def list_mcp_clients(request: web.Request) -> web.Response:
    conn = request.app[CONN_KEY]
    registration = registry.active_registration(conn, request.match_info["discord_user_id"])
    if registration is None:
        return _error(404, "not_found")
    return web.json_response(oauth.list_clients(conn, registration["id"]))


async def delete_mcp_client(request: web.Request) -> web.Response:
    conn = request.app[CONN_KEY]
    with transaction(conn):
        deleted = oauth.delete_client_in_tx(conn, request.match_info["client_id"])
    return web.json_response({"deleted": True}) if deleted else _error(404, "not_found")


async def list_mcp_connections(request: web.Request) -> web.Response:
    conn = request.app[CONN_KEY]
    registration = registry.active_registration(conn, request.match_info["discord_user_id"])
    if registration is None:
        return _error(404, "not_found")
    return web.json_response(oauth.list_connections(conn, registration["id"]))


async def delete_mcp_connections(request: web.Request) -> web.Response:
    """One connection (by id) or all of them. Always allowed, like every DELETE."""
    conn = request.app[CONN_KEY]
    connection_id = request.match_info.get("connection_id")
    with transaction(conn):
        registration = registry.active_registration(conn, request.match_info["discord_user_id"])
        if registration is None:
            return _error(404, "not_found")
        removed = oauth.delete_connections_in_tx(conn, registration["id"], connection_id)
    if connection_id is not None and not removed:
        return _error(404, "not_found")
    return web.json_response({"disconnected": removed})


async def connector_settings(request: web.Request) -> web.Response:
    """What every member enters in grok.com for the shared public client. Nothing here is a credential."""
    config = request.app[CONFIG_KEY]
    if not config.discord_login_enabled:
        return _error(404, "discord_login_off")
    return web.json_response(oauth.connector_settings(config))


# ------------------------------------------------------------------ the claim link

async def mint_claim(request: web.Request) -> web.Response:
    """A new claim link, replacing any earlier one (DESIGN.md, Claiming the bridge). Returned once, never logged."""
    config = request.app[CONFIG_KEY]
    conn = request.app[CONN_KEY]
    if not config.discord_login_enabled:
        return _error(404, "discord_login_off")
    if not config.members_set_automations:
        return _error(409, "operator_mode")
    with transaction(conn):
        if is_paused(conn):
            return web.Response(status=403)
        if claim.is_claimed(conn):
            return _error(409, "claimed")
        secret, expires_at = claim.mint_in_tx(conn, _now(request))
    log.info("claim link minted")
    return web.json_response({"claim_url": f"{config.public_base_url}{claim.CLAIM_PATH}?token={secret}",
                              "expires_at": expires_at})


# ------------------------------------------------------------------ pause and status

async def pause(request: web.Request) -> web.Response:
    conn = request.app[CONN_KEY]
    with transaction(conn):
        withdrawn = registry.set_paused_in_tx(conn, True, _now(request))
    if withdrawn:
        request.app[WAKE_KEY]()
    return web.json_response({"paused": True, "withdrawn": withdrawn})


async def unpause(request: web.Request) -> web.Response:
    conn = request.app[CONN_KEY]
    with transaction(conn):
        registry.set_paused_in_tx(conn, False, _now(request))
    return web.json_response({"paused": False})


async def status(request: web.Request) -> web.Response:
    conn = request.app[CONN_KEY]
    return web.json_response({
        "paused": is_paused(conn),
        "pending_final_messages": registry.pending_final_messages(conn),
    })


async def metrics_endpoint(request: web.Request) -> web.Response:
    """Prometheus text format. Unauthenticated: this listener is loopback only and never proxied."""
    return web.Response(text=metrics.METRICS.render(), content_type="text/plain",
                        headers={"X-Content-Type-Options": "nosniff"})


async def readyz(request: web.Request) -> web.Response:
    """Ready only if the database answers, its file modes are safe, and background tasks run.

    Never probes grok.com or Discord (DESIGN.md, Observability).
    """
    checks: dict[str, str] = {}
    try:
        request.app[CONN_KEY].execute("SELECT 1").fetchone()
        checks["database"] = "ok"
    except Exception:
        checks["database"] = "failed"
    try:
        db.check_file_modes(request.app[CONFIG_KEY].database_path)
        checks["file_modes"] = "ok"
    except (db.DatabaseError, OSError):
        checks["file_modes"] = "failed"
    checks["background_tasks"] = "ok" if request.app[BACKGROUND_KEY]() else "failed"
    ready = all(value == "ok" for value in checks.values())
    return web.json_response({"ready": ready, "checks": checks}, status=200 if ready else 503)


def create_admin_app(config: Config, conn: sqlite3.Connection, wake_finalizer: Callable[[], None],
                     clock: Callable[[], float] = time.time,
                     resolver: Resolver = system_resolver,
                     bind_hosts: Collection[str] = (),
                     background_alive: Callable[[], bool] = lambda: True) -> web.Application:
    """Loopback only: /v1 (bearer), /metrics, and /readyz."""
    app = web.Application(middlewares=[require_bearer], client_max_size=MAX_BODY_BYTES)
    app[CONFIG_KEY] = config
    app[CONN_KEY] = conn
    app[WAKE_KEY] = wake_finalizer
    app[CLOCK_KEY] = clock
    app[RESOLVER_KEY] = resolver
    app[BIND_HOSTS_KEY] = frozenset(bind_hosts)
    app[BACKGROUND_KEY] = background_alive
    app.router.add_get("/metrics", metrics_endpoint)
    app.router.add_get("/readyz", readyz)
    app.router.add_get("/v1/registrations", list_members)
    app.router.add_post("/v1/registrations", upsert_registration)
    app.router.add_delete("/v1/registrations/{discord_user_id}", revoke_registration)
    app.router.add_delete("/v1/registrations/{discord_user_id}/automation", remove_automation)
    app.router.add_post("/v1/registrations/{discord_user_id}/mcp-clients", issue_mcp_client)
    app.router.add_get("/v1/registrations/{discord_user_id}/mcp-clients", list_mcp_clients)
    app.router.add_delete("/v1/mcp-clients/{client_id}", delete_mcp_client)
    app.router.add_get("/v1/registrations/{discord_user_id}/mcp-connections", list_mcp_connections)
    app.router.add_delete("/v1/registrations/{discord_user_id}/mcp-connections", delete_mcp_connections)
    app.router.add_delete("/v1/registrations/{discord_user_id}/mcp-connections/{connection_id}",
                          delete_mcp_connections)
    app.router.add_get("/v1/connector-settings", connector_settings)
    app.router.add_post("/v1/claim", mint_claim)
    app.router.add_post("/v1/pause", pause)
    app.router.add_delete("/v1/pause", unpause)
    app.router.add_get("/v1/status", status)
    return app
