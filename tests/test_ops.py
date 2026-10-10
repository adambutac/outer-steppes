"""PR 6: metrics, /readyz, retention, JSON logs, and the deploy bundle."""

from __future__ import annotations

import importlib.util
import io
import json
import logging
import re
from pathlib import Path

import pytest

from outer_steppes import metrics
from outer_steppes.http_app import KNOWN_COMMANDS, create_public_app
from outer_steppes.interactions import InteractionHandler
from outer_steppes.loopback_api import create_admin_app
from outer_steppes.main import JsonFormatter
from outer_steppes.mcp_server import McpServer
from outer_steppes.oauth import OAuthServer
from outer_steppes.reply import HeldPrompts, ReplyService, mint_reply_token
from outer_steppes.sweeper import apply_retention

from .conftest import CALLER, GUILD, NOW, command, connect_member, link_automations, make_config, register, snowflake

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture(autouse=True)
def fresh_metrics():
    return metrics.reset()


@pytest.fixture
def config(signer, db_path):
    return make_config(signer.public_key, db_path)


# ------------------------------------------------------------------ metrics format

def test_render_is_prometheus_text():
    registry = metrics.METRICS
    registry.interactions.inc("ask", "deferred")
    registry.interactions.inc("ask", "deferred")
    registry.ack_latency.observe(0.07)
    registry.inflight.read = lambda: 3
    text = registry.render()
    assert '# TYPE bridge_interactions_total counter' in text
    assert 'bridge_interactions_total{command="ask",result="deferred"} 2' in text
    assert 'bridge_ack_latency_seconds_bucket{le="0.05"} 0' in text
    assert 'bridge_ack_latency_seconds_bucket{le="0.1"} 1' in text
    assert 'bridge_ack_latency_seconds_bucket{le="+Inf"} 1' in text
    assert "bridge_inflight 3" in text
    for name in ("bridge_automation_post_total", "bridge_automation_latency_seconds", "bridge_reply_total",
                 "bridge_reply_wait_seconds", "bridge_queue_wait_seconds", "bridge_mcp_auth_failures_total"):
        assert f"# TYPE {name}" in text


def test_label_values_are_escaped():
    metrics.METRICS.discord_logins.inc('a"b', "c\\d")
    assert 'purpose="a\\"b",result="c\\\\d"' in metrics.METRICS.render()


# ------------------------------------------------------------------ instrumentation

async def test_interactions_are_counted_by_command_and_result(aiohttp_client, config, conn, signer):
    register(conn, CALLER)
    connect_member(conn, CALLER)
    handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=lambda: NOW)
    client = await aiohttp_client(create_public_app(config, handler, lambda *_: None, clock=lambda: NOW))

    async def post(body: bytes, headers=None):
        return await client.post("/interactions", data=body, headers=headers or signer.headers(body))

    await post(b'{"type":1}')
    await post(json.dumps(command()).encode())
    await post(json.dumps(command("leave")).encode())
    await post(json.dumps(command("nonsense")).encode())
    await post(json.dumps(command("frobnicate")).encode())
    await post(b'{"type":1}', headers={"X-Signature-Ed25519": "00" * 64, "X-Signature-Timestamp": str(NOW)})
    counter = metrics.METRICS.interactions
    assert counter.value("ping", "pong") == 1
    assert counter.value("ask", "deferred") == 1
    assert counter.value("leave", "ephemeral") == 1
    assert counter.value("other", "ephemeral") == 2       # unknown names never become labels
    assert counter.value("other", "bad_signature") == 1
    assert metrics.METRICS.ack_latency.count == 6


def test_every_registered_command_has_its_own_metric_label():
    spec = importlib.util.spec_from_file_location("osctl_command", REPO / "scripts" / "osctl_command.py")
    osctl_command = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(osctl_command)
    registered = {c["name"] for c in osctl_command.build_commands()}
    assert registered == set(KNOWN_COMMANDS)


async def test_replies_fetches_and_mcp_auth_failures_are_counted(aiohttp_client, config, conn, fake_discord):
    registration_id = register(conn, CALLER)
    token, digest = mint_reply_token()
    conn.execute(
        "INSERT INTO interactions (interaction_id, status, ack_kind, guild_id, channel_id, caller_id, "
        "registration_id, owner_id, prompt_mac, interaction_token, reply_token_hash, "
        "reply_expires_at, posted_at, created_at, updated_at) VALUES (?, 'posted', 'defer', ?, 'c', ?, "
        "?, ?, 'q', 'itok', ?, ?, ?, ?, ?)",
        (snowflake(), GUILD, CALLER, registration_id, CALLER, digest, NOW + 600, NOW - 12, NOW, NOW))
    link_automations(conn)
    replies = ReplyService(conn, fake_discord, config, lambda: None, HeldPrompts(), clock=lambda: NOW)
    oauth_server = OAuthServer(conn, config, clock=lambda: NOW)
    handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=lambda: NOW)
    client = await aiohttp_client(create_public_app(config, handler, lambda *_: None, clock=lambda: NOW,
                                                    oauth=oauth_server,
                                                    mcp=McpServer(oauth_server, replies)))
    replies.get_prompt(token, registration_id)  # nothing held: the restart ending
    replies.get_prompt("rt_made_up", registration_id)
    await client.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert metrics.METRICS.prompt_fetches.value("restarted") == 1
    assert metrics.METRICS.prompt_fetches.value("unknown") == 1
    assert metrics.METRICS.mcp_auth_failures.value() == 1

    token, digest = mint_reply_token()
    conn.execute(
        "INSERT INTO interactions (interaction_id, status, ack_kind, guild_id, channel_id, caller_id, "
        "registration_id, owner_id, prompt_mac, interaction_token, reply_token_hash, "
        "reply_expires_at, posted_at, created_at, updated_at) VALUES (?, 'posted', 'defer', ?, 'c', ?, "
        "?, ?, 'q', 'itok', ?, ?, ?, ?, ?)",
        (snowflake(), GUILD, CALLER, registration_id, CALLER, digest, NOW + 600, NOW - 12, NOW, NOW))
    link_automations(conn)
    assert await replies.deliver_reply(token, "hi", registration_id) == "delivered"
    assert await replies.deliver_reply(token, "hi", registration_id) == "used"
    assert metrics.METRICS.replies.value("delivered") == 1
    assert metrics.METRICS.replies.value("used") == 1
    assert metrics.METRICS.reply_wait.count == 1 and metrics.METRICS.reply_wait.sum == 12


def test_post_outcomes_map_to_design_labels():
    from outer_steppes.automation_client import PostClass, PostOutcome, classify_status
    from outer_steppes.worker import _post_result
    assert [_post_result(classify_status(s)) for s in (202, 500, 429, 503, 401, 404)] == [
        "accepted", "ambiguous", "busy", "busy", "rejected_auth", "rejected_other"]
    assert _post_result(PostOutcome(PostClass.NOT_SENT, None, "x")) == "not_sent"


# ------------------------------------------------------------------ admin endpoints

async def test_metrics_and_readyz_on_the_admin_listener(aiohttp_client, config, conn):
    alive = {"value": True}
    app = create_admin_app(config, conn, lambda: None, background_alive=lambda: alive["value"])
    client = await aiohttp_client(app)

    resp = await client.get("/metrics")  # no bearer: loopback-only listener, like /readyz
    assert resp.status == 200 and resp.content_type == "text/plain"
    assert "bridge_interactions_total" in await resp.text()

    ready = await client.get("/readyz")
    assert ready.status == 200
    assert (await ready.json())["checks"] == {"database": "ok", "file_modes": "ok", "background_tasks": "ok"}

    alive["value"] = False
    not_ready = await client.get("/readyz")
    assert not_ready.status == 503 and (await not_ready.json())["checks"]["background_tasks"] == "failed"


async def test_readyz_fails_on_unsafe_file_modes(aiohttp_client, config, conn, monkeypatch):
    from outer_steppes import db

    def unsafe(path):
        raise db.DatabaseError("database file is accessible to group or others")

    monkeypatch.setattr(db, "check_file_modes", unsafe)
    client = await aiohttp_client(create_admin_app(config, conn, lambda: None))
    resp = await client.get("/readyz")
    assert resp.status == 503 and (await resp.json())["checks"]["file_modes"] == "failed"


# ------------------------------------------------------------------ retention

def test_retention_deletes_only_old_terminal_rows(conn):
    day = 86_400

    def interaction(status, created, final=None):
        iid = snowflake()
        conn.execute("INSERT INTO interactions (interaction_id, status, ack_kind, prompt_mac, final_message, "
                     "created_at, updated_at) VALUES (?, ?, 'defer', 'mac', ?, ?, ?)",
                     (iid, status, final, created, created))
        return iid

    old_done = [interaction(s, NOW - day - 1) for s in ("done", "answered", "failed", "expired")]
    old_pending_final = interaction("failed", NOW - day - 1, final="Busy, try again.")
    old_live = interaction("posted", NOW - day - 1)
    recent = interaction("answered", NOW - 10)
    conn.execute("INSERT INTO rate_events (subject_kind, subject_id, at) VALUES ('caller', '1', ?), ('caller', '1', ?)",
                 (NOW - 2 * day - 1, NOW - 10))
    conn.execute("INSERT INTO registrations (id, discord_user_id, revoked_at, created_at, updated_at) VALUES "
                 "('old', '1', ?, 0, 0), ('recent', '2', ?, 0, 0), ('active', '3', NULL, 0, 0)",
                 (NOW - 30 * day - 1, NOW - day))
    conn.execute("INSERT INTO automations (id, registration_id, kind, created_at, removed_at) VALUES "
                 "('a_old', 'active', 'grok_webhook', 0, ?), ('a_recent', 'active', 'grok_webhook', 0, ?)",
                 (NOW - 30 * day - 1, NOW - day))
    conn.execute("INSERT INTO automations (id, registration_id, kind, automation_url, signing_secret, created_at) "
                 "VALUES ('a_live', 'active', 'grok_webhook', 'u', 's', 0)")
    # PR 8c: expired sessions, and save counts older than the hour the rate limit looks at.
    conn.execute("INSERT INTO web_sessions (session_hash, discord_user_id, username, expires_at) VALUES "
                 "('expired', '3', 'm', ?), ('live', '3', 'm', ?)", (NOW, NOW + 1))
    conn.execute("INSERT INTO account_changes (discord_user_id, at) VALUES ('3', ?), ('3', ?)",
                 (NOW - 3600, NOW - 3599))

    removed = apply_retention(conn, NOW)
    assert removed == {"interactions": 4, "rate_events": 1, "registrations": 1, "automations": 1,
                       "idle_members": 0, "web_sessions": 1, "account_changes": 1}
    assert [r[0] for r in conn.execute("SELECT session_hash FROM web_sessions")] == ["live"]
    assert [r[0] for r in conn.execute("SELECT at FROM account_changes")] == [NOW - 3599]
    assert {r[0] for r in conn.execute("SELECT id FROM automations")} == {"a_recent", "a_live"}
    remaining = {r[0] for r in conn.execute("SELECT interaction_id FROM interactions")}
    assert remaining == {old_pending_final, old_live, recent}
    assert not set(old_done) & remaining
    assert {r[0] for r in conn.execute("SELECT id FROM registrations")} == {"recent", "active"}


# ------------------------------------------------------------------ JSON logs

def test_json_log_lines_carry_only_fixed_fields():
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonFormatter())
    logger = logging.getLogger("outer_steppes.test_json")
    logger.addHandler(handler)
    logger.propagate = False
    try:
        logger.warning("reply: %s", "too_long",
                       extra={"interaction_id": "123", "correlation_id": "corr_x", "other": "dropped"})
    finally:
        logger.removeHandler(handler)
    entry = json.loads(stream.getvalue())
    assert entry["msg"] == "reply: too_long"
    assert (entry["level"], entry["logger"]) == ("WARNING", "outer_steppes.test_json")
    assert (entry["interaction_id"], entry["correlation_id"]) == ("123", "corr_x")
    assert "other" not in entry
    assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", entry["ts"])


# ------------------------------------------------------------------ deploy bundle

def documented_public_routes() -> set[tuple[str, str]]:
    """The Public row of DESIGN.md's listener table, as (method, path) pairs."""
    design = (REPO / "DESIGN.md").read_text(encoding="utf-8")
    row = next(line for line in design.splitlines() if line.strip().startswith("| Public |"))
    routes = row.rstrip().rstrip("|").rsplit("|", 1)[1]
    found = re.findall(r"`(?:([A-Z]+)`/`)?([A-Z]+) (/[^`]*)`", routes)
    return {(method, path) for first, second, path in found for method in (first, second) if method}


def test_public_listener_serves_exactly_the_documented_routes(config, conn):
    # Funnel forwards the whole public listener, with no path rules of its own (ADR-0015),
    # so this route table is the only thing keeping a route off the internet.
    handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0)
    replies = ReplyService(conn, None, config, lambda: None, HeldPrompts())
    oauth_server = OAuthServer(conn, config)
    app = create_public_app(config, handler, lambda *_: None, oauth=oauth_server,
                            mcp=McpServer(oauth_server, replies))
    # aiohttp adds HEAD to each GET by itself, and DESIGN.md lists only the GET. Any other HEAD route counts.
    routes = {(r.method, r.resource.canonical) for r in app.router.routes()}
    served = {(m, path) for m, path in routes if not (m == "HEAD" and ("GET", path) in routes)}
    documented = documented_public_routes()
    assert ("GET", "/healthz") in documented and ("DELETE", "/mcp") in documented  # the parse found the row
    assert served == documented
    for admin_path in ("/v1", "/metrics", "/readyz"):
        assert not any(path.startswith(admin_path) for _, path in served)


def test_dockerfile_is_non_root_and_exposes_only_the_public_port():
    dockerfile = (REPO / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    instructions = [line for line in dockerfile.splitlines() if line and not line.startswith("#")]
    assert "USER outer-steppes" in instructions
    assert [line for line in instructions if line.startswith("EXPOSE")] == ["EXPOSE 8080"]
    assert "PUBLIC_BIND=0.0.0.0:8080" in dockerfile and "ADMIN_BIND=127.0.0.1:8081" in dockerfile
    assert "umask 077" in dockerfile and "-m 0700 /var/lib/outer-steppes" in dockerfile
    code = "\n".join(instructions)
    assert "DISCORD_BOT_TOKEN" not in code and "osctl_command" not in code
    dockerignore = (REPO / ".dockerignore").read_text(encoding="utf-8").split()
    assert "scripts/osctl_command.py" in dockerignore and "data" in dockerignore
