"""Signing, payload, response classification, pinning, and no redirects."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
import re
from pathlib import Path

import pytest
from aiohttp import web

from outer_steppes.automation_client import (
    MSG_GROK_BUSY, MSG_KEY_REJECTED, MSG_NOT_ACCEPTED, MSG_SIGNATURE_REJECTED, RUN_INSTRUCTIONS, AutomationClient, PinnedResolver,
    PostClass, build_payload, classify_status, sign_body,
)
from outer_steppes.url_policy import AcceptedUrl

SECRET = "whsec_MDEyMzQ1Njc4OWFiY2RlZg=="
VECTOR_BODY = b'{"correlation_id":"corr_test","reply_token":"rt_test","reply_expires_at":1700000840}'


def test_signing_vector_from_design():
    assert sign_body(SECRET, VECTOR_BODY, "msg_test", "1700000000") == "v1,m+P+mlvOOeGNDDzPDEUu8q0lVXh/hOJZOyh5GtM/jmE="


def test_payload_matches_vector_bytes_and_key_set():
    body = build_payload(correlation_id="corr_test", reply_token="rt_test", reply_expires_at=1700000840)
    # The vector's body leaves out the fixed instructions, for brevity (DESIGN.md, Signing).
    instructions = json.dumps(RUN_INSTRUCTIONS, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    assert body == b'{"instructions":' + instructions + b"," + VECTOR_BODY[1:]
    # No prompt and no Discord ids: the run fetches them with get_prompt (ADR-0017).
    assert list(json.loads(body)) == ["instructions", "correlation_id", "reply_token", "reply_expires_at"]


def test_the_vector_equals_the_design_text():
    design = (Path(__file__).parent.parent / "DESIGN.md").read_text(encoding="utf-8")
    assert f"| Body | `{VECTOR_BODY.decode()}` |" in design
    assert "| Signature | `v1,m+P+mlvOOeGNDDzPDEUu8q0lVXh/hOJZOyh5GtM/jmE=` |" in design


@pytest.mark.parametrize("status,kind,message", [
    (200, PostClass.ACCEPTED, None), (202, PostClass.ACCEPTED, None), (204, PostClass.ACCEPTED, None),
    (500, PostClass.AMBIGUOUS, None), (502, PostClass.AMBIGUOUS, None), (504, PostClass.AMBIGUOUS, None),
    (401, PostClass.NOT_SENT, MSG_SIGNATURE_REJECTED),
    (429, PostClass.NOT_SENT, MSG_GROK_BUSY), (503, PostClass.NOT_SENT, MSG_GROK_BUSY),
    (301, PostClass.NOT_SENT, MSG_NOT_ACCEPTED), (302, PostClass.NOT_SENT, MSG_NOT_ACCEPTED),
    (400, PostClass.NOT_SENT, MSG_NOT_ACCEPTED), (403, PostClass.NOT_SENT, MSG_NOT_ACCEPTED),
    (404, PostClass.NOT_SENT, MSG_NOT_ACCEPTED), (410, PostClass.NOT_SENT, MSG_NOT_ACCEPTED),
    (413, PostClass.NOT_SENT, MSG_NOT_ACCEPTED),
])
def test_classification_table(status, kind, message):
    outcome = classify_status(status)
    assert (outcome.kind, outcome.message) == (kind, message)


# ------------------------------------------------------------------ against a local server

@pytest.fixture
async def automation(aiohttp_server):
    state = {"requests": [], "status": 202, "delay": 0.0, "hit_redirect_target": False}

    async def trigger(request: web.Request) -> web.Response:
        state["requests"].append({"headers": dict(request.headers), "body": await request.read()})
        await asyncio.sleep(state["delay"])
        if state["status"] == 302:
            raise web.HTTPFound("/elsewhere")
        return web.json_response({"note": "a body the bridge must ignore"}, status=state["status"])

    async def elsewhere(request: web.Request) -> web.Response:
        state["hit_redirect_target"] = True
        return web.json_response({})

    app = web.Application()
    app.router.add_post("/webhook/automation/{id}", trigger)
    app.router.add_route("*", "/elsewhere", elsewhere)
    server = await aiohttp_server(app)
    accepted = AcceptedUrl(f"http://grok.com:{server.port}/webhook/automation/00000000-0000-4000-8000-000000000001",
                           "grok", "grok.com", ("127.0.0.1",))
    return accepted, state


def client(timeout=5):
    return AutomationClient(timeout, clock=lambda: 1_700_000_000, ssl=False)


async def test_post_is_signed_over_exact_bytes_and_pinned(automation):
    accepted, state = automation
    outcome = await client().post(accepted, VECTOR_BODY, SECRET)
    assert outcome.kind is PostClass.ACCEPTED and outcome.status == 202
    [request] = state["requests"]
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert request["body"] == VECTOR_BODY
    assert headers["content-type"] == "application/json"
    assert headers["webhook-id"].startswith("msg_") and len(headers["webhook-id"]) == 36
    assert headers["webhook-timestamp"] == "1700000000"
    assert headers["user-agent"].startswith("outer-steppes/")
    assert "authorization" not in headers
    key = base64.b64decode(SECRET.removeprefix("whsec_"))
    signed = f"{headers['webhook-id']}.{headers['webhook-timestamp']}.".encode() + VECTOR_BODY
    expected = "v1," + base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    assert headers["webhook-signature"] == expected


async def test_fresh_webhook_id_per_post(automation):
    accepted, state = automation
    await client().post(accepted, VECTOR_BODY, SECRET)
    await client().post(accepted, VECTOR_BODY, SECRET)
    ids = [r["headers"]["webhook-id"] for r in state["requests"]]
    assert ids[0] != ids[1]


async def test_redirect_is_not_followed(automation):
    accepted, state = automation
    state["status"] = 302
    outcome = await client().post(accepted, VECTOR_BODY, SECRET)
    assert outcome.kind is PostClass.NOT_SENT
    assert state["hit_redirect_target"] is False
    assert len(state["requests"]) == 1


@pytest.mark.parametrize("status,kind", [(429, PostClass.NOT_SENT), (503, PostClass.NOT_SENT),
                                         (401, PostClass.NOT_SENT), (500, PostClass.AMBIGUOUS)])
async def test_status_outcomes_are_never_retried(automation, status, kind):
    accepted, state = automation
    state["status"] = status
    assert (await client().post(accepted, VECTOR_BODY, SECRET)).kind is kind
    assert len(state["requests"]) == 1  # one POST, never a second signature


async def test_timeout_after_sending_is_ambiguous(automation):
    accepted, state = automation
    state["delay"] = 3
    outcome = await client(timeout=1).post(accepted, VECTOR_BODY, SECRET)
    assert outcome.kind is PostClass.AMBIGUOUS
    assert len(state["requests"]) == 1


async def test_connection_refused_is_not_sent():
    accepted = AcceptedUrl("http://grok.com:1/webhook/automation/x", "grok", "grok.com", ("127.0.0.1",))
    assert (await client().post(accepted, VECTOR_BODY, SECRET)).kind is PostClass.NOT_SENT


async def test_pinned_resolver_refuses_any_other_host():
    resolver = PinnedResolver(AcceptedUrl("https://grok.com/x", "grok", "grok.com", ("104.18.0.1",)))
    [result] = await resolver.resolve("grok.com", 443)
    assert result["host"] == "104.18.0.1"
    with pytest.raises(OSError):
        await resolver.resolve("evil.example", 443)


def test_the_run_instructions_equal_the_design_text_word_for_word():
    """DESIGN.md, Payload: the instructions every signed body carries (ADR-0019)."""
    design = (Path(__file__).parent.parent / "DESIGN.md").read_text(encoding="utf-8")
    section = design.split("## Payload", 1)[1].split("### Signing", 1)[0]
    quoted = re.search(r"^> (.*)$", section, re.MULTILINE).group(1)
    assert quoted == RUN_INSTRUCTIONS
    body = json.loads(build_payload(correlation_id="corr_test", reply_token="rt_test", reply_expires_at=1))
    assert body["instructions"] == RUN_INSTRUCTIONS


# ------------------------------------------------------------------ Grok Bot (ADR-0020)

BOT_KEY = "crsr_" + hashlib.sha256(b"made-up grok bot key").hexdigest()  # made up


async def test_a_grok_bot_post_carries_the_key_as_a_bearer_and_no_signature(automation):
    accepted, state = automation
    bot = AcceptedUrl(accepted.url, "grok_bot", accepted.host, accepted.addresses, "bearer")
    outcome = await client().post(bot, VECTOR_BODY, BOT_KEY)
    assert outcome.kind is PostClass.ACCEPTED
    [request] = state["requests"]
    headers = {k.lower(): v for k, v in request["headers"].items()}
    assert request["body"] == VECTOR_BODY
    assert headers["authorization"] == "Bearer " + BOT_KEY
    assert headers["content-type"] == "application/json" and headers["user-agent"].startswith("outer-steppes/")
    assert not any(name.startswith("webhook-") for name in headers)


def test_a_rejected_key_says_key_and_a_rejected_signature_says_signature():
    assert classify_status(401, "bearer").message == MSG_KEY_REJECTED
    assert classify_status(401).message == MSG_SIGNATURE_REJECTED
