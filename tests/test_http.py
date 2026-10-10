from __future__ import annotations

import json

import pytest
from aiohttp import web

from outer_steppes.http_app import MAX_BODY_BYTES, create_public_app
from outer_steppes.interactions import InteractionHandler

from .conftest import CALLER, NOW, command, connect_member, make_config, register, snowflake

PING = b'{"type":1,"id":"1","application_id":"2","version":1}'


class Spawner:
    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def __call__(self, interaction_id: str, prompt: str) -> None:
        self.calls.append((interaction_id, prompt))


@pytest.fixture
def spawner() -> Spawner:
    return Spawner()


@pytest.fixture
def config(signer, db_path):
    return make_config(signer.public_key, db_path)


@pytest.fixture
async def client(aiohttp_client, config, conn, spawner):
    register(conn, CALLER)
    connect_member(conn, CALLER)
    handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=lambda: NOW)
    return await aiohttp_client(create_public_app(config, handler, spawner, clock=lambda: NOW))


def encode(interaction: dict) -> bytes:
    return json.dumps(interaction).encode()


async def post(client, signer, body: bytes, **kw):
    return await client.post("/interactions", data=body, headers=signer.headers(body, **kw))


async def test_healthz(client):
    resp = await client.get("/healthz")
    assert resp.status == 200
    assert await resp.json() == {"status": "ok"}


async def test_signed_ping_returns_pong_as_json(client, signer):
    resp = await post(client, signer, PING)
    assert resp.status == 200
    assert resp.content_type == "application/json"
    assert json.loads(await resp.read()) == {"type": 1}


async def test_ask_defers_and_spawns_worker(client, signer, spawner):
    interaction = command(prompt="what is up")
    resp = await post(client, signer, encode(interaction))
    assert resp.status == 200
    assert resp.content_type == "application/json"
    assert await resp.json() == {"type": 5, "data": {"flags": 64}}  # private by default
    assert spawner.calls == [(interaction["id"], "what is up")]


async def test_public_ask_defers_visibly(client, signer):
    resp = await post(client, signer, encode(command(private=False)))
    assert await resp.json() == {"type": 5}


async def test_refusal_is_ephemeral_type_4_and_does_not_spawn(client, signer, spawner):
    resp = await post(client, signer, encode(command("frobnicate")))
    assert await resp.json() == {
        "type": 4,
        "data": {
            "content": "Unsupported command.",
            "flags": 64,
            "allowed_mentions": {"parse": []},
        },
    }
    assert spawner.calls == []


async def test_bad_signature_is_401(client, signer):
    headers = signer.headers(PING)
    resp = await client.post("/interactions", data=b'{"type":1,"id":"9"}', headers=headers)
    assert resp.status == 401
    assert await resp.text() == "invalid request signature"


@pytest.mark.parametrize("missing", ["X-Signature-Ed25519", "X-Signature-Timestamp"])
async def test_missing_signature_header_is_401(client, signer, missing):
    headers = signer.headers(PING)
    del headers[missing]
    resp = await client.post("/interactions", data=PING, headers=headers)
    assert resp.status == 401


async def test_stale_timestamp_is_401(client, signer):
    resp = await post(client, signer, PING, timestamp=NOW - 301)
    assert resp.status == 401


async def test_oversized_body_is_413(client, signer):
    body = b'{"type":1,"pad":"' + b"x" * MAX_BODY_BYTES + b'"}'
    assert (await post(client, signer, body)).status == 413


async def test_body_at_cap_is_accepted(client, signer):
    prefix, suffix = b'{"type":1,"pad":"', b'"}'
    body = prefix + b"x" * (MAX_BODY_BYTES - len(prefix) - len(suffix)) + suffix
    assert len(body) == MAX_BODY_BYTES
    assert (await post(client, signer, body)).status == 200


@pytest.mark.parametrize("body", [b"not json", b"[1,2]", b'{"type":"1"}', b"{}", b'{"type":true}'])
async def test_signed_but_malformed_json_is_400(client, signer, body):
    assert (await post(client, signer, body)).status == 400


async def test_get_interactions_is_not_allowed(client):
    assert (await client.get("/interactions")).status == 405


async def test_worker_spawns_only_after_response_is_written(aiohttp_client, config, conn, signer,
                                                            monkeypatch):
    """DESIGN.md ACK step 13: no Discord call can precede the deferral."""
    events: list[str] = []
    original_write_eof = web.StreamResponse.write_eof

    async def recording_write_eof(self, *args, **kwargs):
        result = await original_write_eof(self, *args, **kwargs)
        events.append("response_written")
        return result

    monkeypatch.setattr(web.StreamResponse, "write_eof", recording_write_eof)
    register(conn, CALLER)
    connect_member(conn, CALLER)
    handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=lambda: NOW)
    app = create_public_app(config, handler, lambda *_: events.append("spawn"), clock=lambda: NOW)
    client = await aiohttp_client(app)

    await post(client, signer, encode(command()))
    assert events[:2] == ["response_written", "spawn"]


# ------------------------------------------------------------------ listeners

def route_table(app: web.Application) -> set[tuple[str, str]]:
    return {(r.method, r.resource.canonical) for r in app.router.routes()}


def test_public_app_serves_only_public_routes(config, conn):
    handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0)
    app = create_public_app(config, handler, lambda *_: None)
    assert route_table(app) == {("GET", "/healthz"), ("HEAD", "/healthz"), ("POST", "/interactions")}


@pytest.mark.parametrize("path", ["/v1/registrations", "/v1/anything", "/metrics", "/readyz"])
async def test_admin_paths_are_404_on_the_public_listener(client, path):
    assert (await client.get(path)).status == 404
    assert (await client.post(path, data=b"{}")).status == 404


async def test_leave_wakes_finalizer_only_after_response_is_written(aiohttp_client, config, conn, signer,
                                                                    monkeypatch):
    """DESIGN.md: withdrawal PATCHes run after the /leave ACK has been written."""
    events: list[str] = []
    original_write_eof = web.StreamResponse.write_eof

    async def recording_write_eof(self, *args, **kwargs):
        result = await original_write_eof(self, *args, **kwargs)
        events.append("response_written")
        return result

    monkeypatch.setattr(web.StreamResponse, "write_eof", recording_write_eof)
    registration_id = register(conn, CALLER)
    for _ in range(3):
        conn.execute(
            "INSERT INTO interactions (interaction_id, status, ack_kind, registration_id, interaction_token, "
            "created_at, updated_at) VALUES (?, 'posted', 'defer', ?, 'tok', ?, ?)",
            (snowflake(), registration_id, NOW, NOW),
        )
    handler = InteractionHandler(config, conn, posts_in_flight=lambda: 0, clock=lambda: NOW)
    app = create_public_app(config, handler, lambda *_: None, lambda: events.append("wake"), clock=lambda: NOW)
    client = await aiohttp_client(app)

    resp = await post(client, signer, encode(command("leave", confirm=True)))
    assert (await resp.json())["data"]["content"].startswith("You've left Outer Steppes.")
    assert events[:2] == ["response_written", "wake"]


# ------------------------------------------------------------------ no HTTP reply path

async def test_there_is_no_http_reply_path(client):
    """ADR-0017: a reply token alone can't post, so POST /reply doesn't exist."""
    resp = await client.post("/reply", json={"reply_token": "rt_made_up", "content": "x"})
    assert resp.status == 404
