"""The /mcp server and OAuth routes over HTTP, replaying grok.com's observed flow."""

from __future__ import annotations

import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit

import pytest

from outer_steppes import oauth
from outer_steppes.db import transaction
from outer_steppes.http_app import create_public_app
from outer_steppes.interactions import InteractionHandler
from outer_steppes.mcp_server import GET_PROMPT_TOOL, TOOL, McpServer
from outer_steppes.oauth import OAuthServer, pkce_s256
from outer_steppes.reply import HeldPrompts, ReplyService, mint_reply_token

from .conftest import CALLER, GUILD, NOW, link_automations, make_config, register, snowflake

CALLBACK = "https://grok.com/connectors-oauth-exchange-code/"
VERIFIER = "x" * 64


@pytest.fixture
def config(db_path):
    return make_config(database_path=db_path)


@pytest.fixture
def held():
    return HeldPrompts()


@pytest.fixture
async def http(aiohttp_client, config, conn, fake_discord, held):
    replies = ReplyService(conn, fake_discord, config, lambda: None, held, clock=lambda: NOW)
    server = OAuthServer(conn, config, clock=lambda: NOW)
    handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=lambda: NOW)
    app = create_public_app(config, handler, lambda *_: None, clock=lambda: NOW, oauth=server,
                            mcp=McpServer(server, replies))
    return await aiohttp_client(app)


@pytest.fixture
def credentials(conn):
    registration_id = register(conn)
    with transaction(conn):
        return oauth.issue_client_in_tx(conn, registration_id, NOW)


async def connect(http, credentials) -> str:
    """The grok.com connector flow as observed by the PR 5 probe. Returns an access token."""
    client_id, secret = credentials
    query = urlencode({"response_type": "code", "client_id": client_id, "redirect_uri": CALLBACK, "state": "s1",
                       "code_challenge": pkce_s256(VERIFIER), "code_challenge_method": "S256",
                       "resource": "https://bridge.example/mcp", "scope": "mcp"})
    resp = await http.get("/oauth/authorize?" + query, allow_redirects=False)
    assert resp.status == 302 and resp.headers["Cache-Control"] == "no-store"
    code = parse_qs(urlsplit(resp.headers["Location"]).query)["code"][0]
    resp = await http.post("/oauth/token", data={
        "grant_type": "authorization_code", "code": code, "redirect_uri": CALLBACK, "code_verifier": VERIFIER,
        "resource": "https://bridge.example/mcp", "client_id": client_id, "client_secret": secret})
    assert resp.status == 200 and resp.headers["Cache-Control"] == "no-store"
    return (await resp.json())["access_token"]


async def rpc(http, token, method, params=None, msg_id=1, version=None):
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json, text/event-stream"}
    if version:
        headers["Mcp-Protocol-Version"] = version
    body = {"jsonrpc": "2.0", "method": method}
    if msg_id is not None:
        body["id"] = msg_id
    if params is not None:
        body["params"] = params
    return await http.post("/mcp", json=body, headers=headers)


def posted_row(conn, held=None, registration_id=None):
    """A posted row; with `held`, its prompt is held in memory, as the spawn does."""
    token, digest = mint_reply_token()
    iid = snowflake()
    conn.execute(
        "INSERT INTO interactions (interaction_id, status, ack_kind, guild_id, channel_id, caller_id, "
        "registration_id, owner_id, prompt_mac, interaction_token, reply_token_hash, "
        "reply_expires_at, created_at, updated_at) VALUES (?, 'posted', 'defer', ?, 'c', ?, ?, ?, "
        "'mac', 'itok', ?, ?, ?, ?)",
        (iid, GUILD, CALLER, registration_id or "reg-" + CALLER, CALLER, digest, NOW + 600, NOW, NOW))
    link_automations(conn)
    if held is not None:
        held.hold(iid, "what is assigned to me")
    return token


async def call_tool(http, token, name, **arguments):
    return (await (await rpc(http, token, "tools/call", {"name": name, "arguments": arguments},
                             version="2025-06-18")).json())["result"]


# ------------------------------------------------------------------ the full grok.com flow

async def test_grok_flow_end_to_end(http, conn, credentials, fake_discord, held):
    token = await connect(http, credentials)

    init = await (await rpc(http, token, "initialize", {
        "protocolVersion": "2025-11-25", "clientInfo": {"name": "connectors-manager", "version": "0.1.0"},
        "capabilities": {}})).json()
    assert init["result"]["protocolVersion"] == "2025-06-18"  # grok.com accepts this downgrade
    assert init["result"]["serverInfo"]["name"] == "outer-steppes"
    instructions = init["result"]["instructions"]
    assert instructions.index("get_prompt") < instructions.index("discord_reply")  # fetch first, then answer
    assert "Do not ask for confirmation" in instructions

    assert (await rpc(http, token, "notifications/initialized", msg_id=None, version="2025-06-18")).status == 202

    tools = (await (await rpc(http, token, "tools/list", {"_meta": {}}, version="2025-06-18")).json())["result"]
    assert [t["name"] for t in tools["tools"]] == ["get_prompt", "discord_reply"]
    assert tools["tools"][0]["inputSchema"]["required"] == ["reply_token"]
    assert tools["tools"][1]["inputSchema"]["required"] == ["reply_token", "content"]

    reply_token = posted_row(conn, held)
    fetched = await call_tool(http, token, "get_prompt", reply_token=reply_token)
    assert fetched["isError"] is False
    assert json.loads(fetched["content"][0]["text"]) == {"prompt": "what is assigned to me",
                                                         "discord_user_id": CALLER, "channel_id": "c"}
    call = await (await rpc(http, token, "tools/call", {
        "name": "discord_reply", "arguments": {"reply_token": reply_token, "content": "Two issues: A and B"}},
        version="2025-06-18")).json()
    assert call["result"] == {"content": [{"type": "text", "text": "Posted to Discord."}], "isError": False}
    assert fake_discord.calls == [("itok", "> what is assigned to me\nTwo issues: A and B")]
    assert conn.execute("SELECT last_used_at FROM oauth_clients").fetchone()[0] == NOW


async def test_validator_protocol_version_is_echoed(http, credentials):
    token = await connect(http, credentials)
    init = await (await rpc(http, token, "initialize", {"protocolVersion": "2025-03-26"})).json()
    assert init["result"]["protocolVersion"] == "2025-03-26"


# ------------------------------------------------------------------ authorization boundaries

async def test_missing_or_bad_bearer_is_401(http):
    resp = await http.post("/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
    assert resp.status == 401 and resp.headers["WWW-Authenticate"] == 'Bearer realm="outer-steppes"'
    resp = await rpc(http, "mcp_nope", "tools/list")
    assert resp.status == 401 and 'error="invalid_token"' in resp.headers["WWW-Authenticate"]


async def test_reply_token_of_another_registration_is_plain_unknown_and_ends_the_ask(http, conn, credentials, held,
                                                                                     fake_discord):
    """A call through another member's connector gets the same `unknown` as before; the live ask ends (T11b)."""
    token = await connect(http, credentials)
    other = register(conn, "42")
    reply_token = posted_row(conn, held, registration_id=other)
    fetched = await call_tool(http, token, "get_prompt", reply_token=reply_token)
    assert fetched == {"content": [{"type": "text", "text": "unknown: this reply_token is not valid for this "
                                                            "connector."}], "isError": True}
    assert conn.execute("SELECT status FROM interactions").fetchone()[0] == "failed"
    call = await (await rpc(http, token, "tools/call", {
        "name": "discord_reply", "arguments": {"reply_token": reply_token, "content": "x"}})).json()
    assert call["result"]["isError"] is True and call["result"]["content"][0]["text"].startswith("unknown:")
    assert fake_discord.calls == []


async def test_connector_without_a_live_reply_token_reads_and_posts_nothing(http, credentials, fake_discord):
    token = await connect(http, credentials)
    call = await (await rpc(http, token, "tools/call", {
        "name": "discord_reply", "arguments": {"reply_token": "rt_made_up", "content": "spam"}})).json()
    assert call["result"]["isError"] is True
    assert (await call_tool(http, token, "get_prompt", reply_token="rt_made_up"))["isError"] is True
    assert fake_discord.calls == []


async def test_get_prompt_errors_say_to_stop(http, conn, credentials, held):
    token = await connect(http, credentials)
    missing = await call_tool(http, token, "get_prompt")
    assert missing["isError"] and missing["content"][0]["text"].startswith("bad_request:")
    restarted = await call_tool(http, token, "get_prompt", reply_token=posted_row(conn))  # nothing held
    assert restarted["isError"] and restarted["content"][0]["text"].startswith("restarted:")
    assert conn.execute("SELECT final_message FROM interactions").fetchone()[0] == (
        "The bridge restarted before Grok could read your message. Please ask again.")
    held_ok = await call_tool(http, token, "get_prompt", reply_token=posted_row(conn, held, "reg-" + CALLER))
    assert not held_ok["isError"]  # the same connector, with the prompt held


def test_the_tool_definitions_equal_the_design_text():
    """DESIGN.md, MCP server: the two JSON blocks are the definitions grok.com sees."""
    design = (Path(__file__).parent.parent / "DESIGN.md").read_text(encoding="utf-8")
    blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", design, re.DOTALL)]
    by_name = {b["name"]: b for b in blocks if isinstance(b, dict) and "inputSchema" in b}
    assert by_name == {"get_prompt": GET_PROMPT_TOOL, "discord_reply": TOOL}


async def test_tool_errors_explain_what_to_do(http, conn, credentials, held):
    token = await connect(http, credentials)
    reply_token = posted_row(conn)
    too_long = await (await rpc(http, token, "tools/call", {
        "name": "discord_reply", "arguments": {"reply_token": reply_token, "content": "x" * 7001}})).json()
    assert too_long["result"]["content"][0]["text"].startswith("too_long: the answer is over 7000 characters")
    ok = await (await rpc(http, token, "tools/call", {
        "name": "discord_reply", "arguments": {"reply_token": reply_token, "content": "short"}})).json()
    assert ok["result"]["isError"] is False
    again = await (await rpc(http, token, "tools/call", {
        "name": "discord_reply", "arguments": {"reply_token": reply_token, "content": "short"}})).json()
    assert again["result"]["content"][0]["text"].startswith("used:")


# ------------------------------------------------------------------ protocol edges

async def test_protocol_errors(http, credentials):
    token = await connect(http, credentials)
    headers = {"Authorization": f"Bearer {token}"}
    assert (await http.post("/mcp", data=b"not json", headers=headers)).status == 400
    batch = await http.post("/mcp", json=[{"jsonrpc": "2.0", "id": 1, "method": "ping"}], headers=headers)
    assert batch.status == 400
    unknown = await (await rpc(http, token, "resources/list")).json()
    assert unknown["error"]["code"] == -32601
    bad_tool = await (await rpc(http, token, "tools/call", {"name": "delete_everything"})).json()
    assert bad_tool["error"]["code"] == -32602
    assert (await (await rpc(http, token, "ping")).json())["result"] == {}


@pytest.mark.parametrize("method", ["GET", "DELETE"])
async def test_no_sse_stream_or_sessions(http, method):
    resp = await http.request(method, "/mcp")
    assert resp.status == 405 and resp.headers["Allow"] == "POST"


async def test_oauth_http_edges(http, credentials):
    client_id, _ = credentials
    repeated = "/oauth/authorize?client_id=a&client_id=b&redirect_uri=" + CALLBACK
    assert (await http.get(repeated, allow_redirects=False)).status == 400
    bad_callback = await http.get(f"/oauth/authorize?client_id={client_id}&redirect_uri=https://evil.example/",
                                  allow_redirects=False)
    assert bad_callback.status == 400 and "Location" not in bad_callback.headers
    json_body = await http.post("/oauth/token", json={"grant_type": "authorization_code"})
    assert json_body.status == 400
    wrong_secret = await http.post("/oauth/token", data={"grant_type": "authorization_code", "code": "x",
                                                         "client_id": client_id, "client_secret": "oss_wrong"})
    assert wrong_secret.status == 401 and "Basic" in wrong_secret.headers["WWW-Authenticate"]


async def test_tokens_and_secrets_never_logged(http, conn, credentials, caplog):
    with caplog.at_level("DEBUG"):
        token = await connect(http, credentials)
        reply_token = posted_row(conn)
        await rpc(http, token, "tools/call", {"name": "discord_reply",
                                              "arguments": {"reply_token": reply_token, "content": "secret answer"}})
    ours = "\n".join(r.getMessage() + str(r.__dict__) for r in caplog.records if r.name.startswith("outer_steppes"))
    for value in (token, credentials[1], reply_token, "secret answer", VERIFIER):
        assert value not in ours


async def test_a_bearer_the_bridge_doesnt_know_is_told_why_and_what_to_do(http):
    """PR 8d-2. With no Authorization header the challenge stays bare (RFC 6750); with a dead bearer it names the error."""
    from outer_steppes.oauth import MSG_CONNECTION_INVALID
    resp = await rpc(http, "mcp_gone", "tools/list")
    assert resp.status == 401
    assert resp.headers["WWW-Authenticate"] == (
        f'Bearer realm="outer-steppes", error="invalid_token", error_description="{MSG_CONNECTION_INVALID}"')
