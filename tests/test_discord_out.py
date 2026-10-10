"""DiscordClient against a local fake of Discord's webhook endpoint."""

from __future__ import annotations

import aiohttp
import pytest
from aiohttp import web

from outer_steppes import discord_out
from outer_steppes.discord_out import DiscordClient, Outcome

APP_ID = "444"
TOKEN = "tok.en-123"


@pytest.fixture
def fake_discord_app():
    state = {"requests": [], "responses": []}

    async def edit(request: web.Request) -> web.Response:
        state["requests"].append({
            "method": request.method,
            "path": request.path,
            "body": await request.json(),
            "auth": request.headers.get("Authorization"),
        })
        status, headers, body = state["responses"].pop(0) if state["responses"] else (200, {}, {})
        return web.json_response(body, status=status, headers=headers)

    app = web.Application()
    app.router.add_route("PATCH", "/api/v10/webhooks/{app}/{token}/messages/@original", edit)
    return app, state


@pytest.fixture
async def client(aiohttp_server, fake_discord_app, monkeypatch):
    app, state = fake_discord_app
    server = await aiohttp_server(app)
    monkeypatch.setattr(discord_out, "MAX_RATE_LIMIT_WAIT_SECONDS", 1.0)
    async with aiohttp.ClientSession() as session:
        yield DiscordClient(session, APP_ID, api_base=str(server.make_url("/api/v10"))), state


async def test_edit_original_sends_no_mentions_and_no_bot_auth(client):
    discord, state = client
    result = await discord.edit_original(TOKEN, "hello <@1>")
    assert result.outcome is Outcome.OK
    [req] = state["requests"]
    assert req["method"] == "PATCH"
    assert req["path"] == f"/api/v10/webhooks/{APP_ID}/{TOKEN}/messages/@original"
    assert req["body"] == {"content": "hello <@1>", "allowed_mentions": {"parse": []}}
    assert req["auth"] is None


@pytest.mark.parametrize("status,outcome", [
    (404, Outcome.NOT_FOUND), (401, Outcome.UNAUTHORIZED), (500, Outcome.FAILED), (400, Outcome.FAILED),
])
async def test_status_mapping(client, status, outcome):
    discord, state = client
    state["responses"] = [(status, {}, {})]
    assert (await discord.edit_original(TOKEN, "x")).outcome is outcome


async def test_429_is_retried_once_when_short(client):
    discord, state = client
    state["responses"] = [(429, {"Retry-After": "0.01"}, {}), (200, {}, {})]
    assert (await discord.edit_original(TOKEN, "x")).outcome is Outcome.OK
    assert len(state["requests"]) == 2


async def test_429_twice_fails(client):
    discord, state = client
    state["responses"] = [(429, {"Retry-After": "0.01"}, {}), (429, {"Retry-After": "0.01"}, {})]
    assert (await discord.edit_original(TOKEN, "x")).outcome is Outcome.FAILED
    assert len(state["requests"]) == 2


async def test_429_with_long_wait_is_not_retried(client):
    discord, state = client
    state["responses"] = [(429, {"Retry-After": "30"}, {})]
    assert (await discord.edit_original(TOKEN, "x")).outcome is Outcome.FAILED
    assert len(state["requests"]) == 1


async def test_network_error_is_failed():
    async with aiohttp.ClientSession() as session:
        discord = DiscordClient(session, APP_ID, api_base="http://127.0.0.1:1/api/v10")
        assert (await discord.edit_original(TOKEN, "x")).outcome is Outcome.FAILED


async def test_token_never_logged(client, caplog):
    discord, state = client
    state["responses"] = [(500, {}, {})]
    with caplog.at_level("DEBUG"):
        await discord.edit_original(TOKEN, "x")
    # Only the bridge's own loggers. The fake Discord server's aiohttp access
    # log does print the path (with the token), which is why the bridge's own
    # listeners run with access_log=None.
    ours = [r for r in caplog.records if r.name.startswith("outer_steppes")]
    assert ours, "expected the failure to be logged"
    assert all(TOKEN not in r.getMessage() and TOKEN not in str(r.__dict__) for r in ours)
