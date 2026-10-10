"""Replay the recorded grok.com connector traffic against the real service (DESIGN.md, Testing, "Recorded traffic").

The recording (fixtures/grok_connector/) is what the real connector sent and what it got back, as the capture's access log kept it.
Each test drives the real service through the same sequence, with the recorded request headers and query shape,
and checks that the bridge still answers as the real client saw: the status, every response header the log kept,
and the size of each response whose body is deterministic. A change here is a change in what grok.com sees, which
is the cue to re-record (DESIGN.md, Testing).

Values the log doesn't hold, or holds only scrubbed, are the test's own: the PKCE pair, `state`, tokens, the Discord
login, and the request bodies (access logs have none; the bodies are the inferred JSON-RPC calls, see the README).

Two recorded steps aren't replayed as such. Step 0 (`/status`) is Discord's traffic, not the connector's, and its
reply depends on the member's state at that moment. The two unauthenticated probes (steps 1-2) differ in size, but
the bridge answers 401 before reading a body, so both get the same `initialize`.
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit

from outer_steppes import __version__

from ..conftest import AUTOMATION_URL, SECRET, command, snowflake
from .clients import GROK_CALLBACK, Browser, Connector, Reply, s256
from .conftest import eventually

RECORDING = Path(__file__).parent / "fixtures" / "grok_connector" / "connector_2026-10-02.jsonl"
USER = "555555555555555555"
# The bridge's version when the recording was made. `initialize` reports it in serverInfo, so a release whose
# version string has another length changes that response's size; the replay allows for exactly that difference
# (a version bump isn't a change in what grok.com relies on, and needs no re-recording).
RECORDED_SERVER_VERSION = "0.1.0"

# Response headers left out of the comparison: per-run or per-version ones (Date, Server), and Location, which the
# capture's log filter deleted (it carries codes); the tests check Location themselves.
NOT_COMPARED = {"server", "date", "location"}
# Request headers not replayed: the proxy's, the transport's, and those whose recorded value is scrubbed and
# the test supplies itself (Authorization, Cookie, Discord's signature).
NOT_REPLAYED = {"x-forwarded-for", "x-forwarded-host", "x-forwarded-proto", "accept-encoding", "content-length",
                "authorization", "cookie", "x-signature-ed25519", "x-signature-timestamp"}


def recording() -> list[dict]:
    lines = RECORDING.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def step(rec: dict) -> tuple[str, str, int]:
    return rec["request"]["method"], urlsplit(rec["request"]["uri"]).path, rec["status"]


def replayed_headers(rec: dict, **extra: str) -> dict:
    headers = {k: v[0] for k, v in rec["request"]["headers"].items() if k.lower() not in NOT_REPLAYED}
    return {**headers, **extra}


def compact(obj: dict) -> bytes:
    return json.dumps(obj, separators=(",", ":")).encode()


def expect(rec: dict, reply: Reply, *, size_delta: int = 0) -> None:
    """The bridge's reply matches the recorded one: status, each header the log kept (a REDACTED value only has to
    be present), no header the real client didn't get, and the body's length (plus `size_delta`)."""
    where = " ".join(map(str, step(rec)))
    assert reply.status == rec["status"], (where, reply.status, reply.text)
    recorded = {k.lower(): v for k, v in rec["resp_headers"].items() if k.lower() not in NOT_COMPARED}
    if "content-length" in recorded:
        recorded["content-length"] = [str(int(recorded["content-length"][0]) + size_delta)]
    for name, values in recorded.items():
        got = reply.headers.getall(name, [])
        if values == ["REDACTED"]:
            assert got, (where, name)
        else:
            assert got == values, (where, name, got, values)
    extra = {k.lower() for k in reply.headers} - recorded.keys() - NOT_COMPARED - {"content-length"}
    assert not extra, (where, "headers the real client didn't get", extra)


async def interact_raw(h, interaction: dict) -> Reply:
    """A signed interaction, keeping the raw reply (Harness.interact parses it, and the size matters here)."""
    body = json.dumps({**interaction, "id": snowflake(time.time())}).encode()
    headers = h.signer.headers(body, timestamp=int(time.time()))
    async with h.http.post(h.public + "/interactions", data=body, headers=headers) as resp:
        return Reply(resp.status, resp.headers.copy(), await resp.text())


class RecordedClient:
    """The connector as the recording shows it: the recorded headers on every request, the test's own values."""

    def __init__(self, h) -> None:
        self.h = h
        self.connector = Connector(h)  # for the PKCE verifier and the resource

    async def mcp(self, rec: dict, message: dict, access: str | None = None) -> Reply:
        headers = replayed_headers(rec)
        if access:
            headers["Authorization"] = f"Bearer {access}"
        return await self.h.call("POST", "/mcp", headers=headers, data=compact(message))

    async def session(self, recs: list[dict], access: str, last: dict, last_size_delta: int = 0) -> None:
        """One MCP session as grok.com runs it: initialize, notifications/initialized, then one more call."""
        init, initialized, call = recs
        started = await self.mcp(init, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": INIT}, access)
        expect(init, started,
               size_delta=len(__version__) - len(RECORDED_SERVER_VERSION) + INITIALIZE_GROWTH_SINCE_RECORDING)
        # The version grok.com sent on its next call is the one the bridge negotiated in the recording.
        assert started.json()["result"]["protocolVersion"] == initialized["request"]["headers"]["Mcp-Protocol-Version"][0]
        expect(initialized, await self.mcp(initialized, NOTIFY_INITIALIZED, access))
        expect(call, await self.mcp(call, last, access), size_delta=last_size_delta)


# grok.com asks for 2025-11-25 and accepts the bridge's 2025-06-18 (DESIGN.md, MCP server); the later calls carry
# Mcp-Protocol-Version: 2025-06-18 in the recording, which is the negotiated version.
INIT = {"protocolVersion": "2025-11-25", "capabilities": {}, "clientInfo": {"name": "grok", "version": "0"}}
NOTIFY_INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
# The JSON-RPC ids are inferred too: the bridge echoes each id, and the access log doesn't hold them. 0, 1 and 2 are
# single-digit ids, like the ones the recorded response sizes imply (405, 715 and 118 bytes); a client's real ids
# could differ in length, which would only shift those sizes.
TOOLS_LIST = {"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}}

# Two sizes changed by design after the recording, updated here by hand (DESIGN.md -> Testing, "Recorded traffic"):
# the build that added get_prompt (ADR-0017) rewrote the initialize instructions to say to fetch first (83 bytes
# longer, as JSON) and added that tool to tools/list (its definition, as JSON, plus ", "). The recording stays as
# grok.com saw it; the fetch itself isn't in it, so a run calling get_prompt first stays a live check.
INITIALIZE_GROWTH_SINCE_RECORDING = 83
TOOLS_LIST_GROWTH_SINCE_RECORDING = 500
# Changed by design since the recording: the public client's authorize step now stops on the connect-time warning
# (DESIGN.md, Connect-time warning; MALU-264), a 200 page with a Content-Type and no Location, where the recording
# has a bodiless 302. Only the bridge's own answer changed, not what grok.com or the browser sends, so this is
# updated here rather than re-recorded (DESIGN.md, Testing, "Recorded traffic"). The page's size is fixed: Discord's
# state is always 43 characters, and its other values (grok.com's state and callback, the Discord application id,
# the base URL) come from this test and its config.
AUTHORIZE_PAGE_SIZE = 2202


def authorize_as_now(rec: dict) -> dict:
    """The recorded authorize step, with the answer the connect-time warning now gives in its place."""
    headers = {**rec["resp_headers"], "Content-Type": ["text/html; charset=utf-8"],
               "Content-Length": [str(AUTHORIZE_PAGE_SIZE)]}
    return {**rec, "status": 200, "resp_headers": headers}


def tools_call(reply_token: str, content: str = "four") -> dict:
    return {"jsonrpc": "2.0", "id": 2, "method": "tools/call",
            "params": {"name": "discord_reply", "arguments": {"reply_token": reply_token, "content": content}}}


def test_the_recording_has_the_sequence_these_tests_replay():
    # If a re-recording changes the sequence, these tests must change with it: this says where.
    assert [step(r) for r in recording()] == [
        ("POST", "/interactions", 200),                              # 0  /status
        ("POST", "/mcp", 401), ("POST", "/mcp", 401),                 # 1-2  unauthenticated probes
        ("GET", "/.well-known/oauth-protected-resource", 404),       # 3  discovery, not served
        ("GET", "/.well-known/oauth-authorization-server", 404),     # 4
        ("GET", "/oauth/authorize", 302),                            # 5  the connect window
        ("GET", "/oauth/discord/callback", 302),                     # 6
        ("POST", "/oauth/token", 200),                               # 7  code exchange
        ("POST", "/interactions", 200),                              # 8  /ask
        *[("POST", "/mcp", s) for s in (200, 202, 200, 200, 202, 200)],  # 9-14  two sessions, interleaved
        *[("POST", "/mcp", s) for s in (200, 202, 200)],             # 15-17  the session that answers
        ("POST", "/oauth/token", 200),                               # 18  refresh, unprompted
        ("POST", "/interactions", 200),                              # 19  the second /ask
        *[("POST", "/mcp", s) for s in (200, 202, 200)],             # 20-22  one session, no tools/list
    ]


async def connect(h, client: RecordedClient, rec: list[dict]) -> dict:
    """Steps 1-7: the probes, discovery, the connect window, and the code exchange. Returns the token response."""
    connector = client.connector
    for probe in rec[1:3]:
        expect(probe, await client.mcp(probe, {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": INIT}))

    for discovery in rec[3:5]:
        # The bridge must not serve these, or grok.com would stop using the manually entered endpoints
        # (DESIGN.md, Connector client behavior: Discovery).
        assert (await h.call("GET", urlsplit(discovery["request"]["uri"]).path)).status == 404

    authorize = rec[5]
    query = [(k, {"code_challenge": s256(connector.verifier), "state": "grok-state",
                  "resource": connector.resource}.get(k, v))
             for k, v in parse_qsl(urlsplit(authorize["request"]["uri"]).query)]
    started = await h.call("GET", "/oauth/authorize?" + urlencode(query), headers=replayed_headers(authorize))
    expect(authorize_as_now(authorize), started)
    assert started.location is None and started.link("Continue").startswith("https://discord.com/")

    callback = rec[6]
    code = h.discord.login_code(USER, "member")
    discord_state = started.link_query("Continue")["state"][0]
    returned = await h.call("GET", f"/oauth/discord/callback?state={discord_state}&code={code}",
                            headers=replayed_headers(callback, Cookie=f"__Host-os_login={started.cookie('__Host-os_login')}"))
    expect(callback, returned)
    assert returned.location.startswith(GROK_CALLBACK) and returned.query()["state"] == "grok-state"

    exchange = rec[7]
    exchanged = await h.call("POST", "/oauth/token", headers=replayed_headers(exchange), data={
        "grant_type": "authorization_code", "code": returned.query()["code"], "redirect_uri": GROK_CALLBACK,
        "code_verifier": connector.verifier, "client_id": "outer-steppes", "resource": connector.resource})
    expect(exchange, exchanged)
    return exchanged.json()


async def set_webhook(h) -> None:
    """Not in the recording: the member saves their automation on the account page, so an /ask has somewhere to go."""
    browser = Browser(h)
    await browser.sign_in(USER)
    saved = await browser.post("/account/webhook", {"automation_url": AUTOMATION_URL, "signing_secret": SECRET})
    assert saved.status == 303


async def ask(h, rec: dict, n: int) -> str:
    """An /ask, checked against the recorded ACK, and the reply token the automation was given."""
    expect(rec, await interact_raw(h, command(caller=USER, prompt=f"question {n}")))
    posts = await eventually(lambda: h.automation.posts[n - 1:n] or None, f"the automation to get /ask {n}")
    return posts[0]["reply_token"]


async def test_a_fresh_connect_and_an_answer_replay_as_recorded(boot):
    h = await boot(login=True, claimed_by=USER)
    rec = recording()
    client = RecordedClient(h)
    tokens = await connect(h, client, rec)
    await set_webhook(h)
    reply_token = await ask(h, rec[8], 1)

    # Two sessions at once, each initialize -> initialized -> tools/list. They run concurrently, as recorded; the
    # exact interleaving of the recording (init, notify, init, list, notify, list) isn't reproduced.
    first, second = [rec[9], rec[10], rec[12]], [rec[11], rec[13], rec[14]]
    assert {r["request"]["headers"]["X-Forwarded-For"][0] for r in first} != {
        r["request"]["headers"]["X-Forwarded-For"][0] for r in second}  # two grok.com machines
    await asyncio.gather(
        client.session(first, tokens["access_token"], TOOLS_LIST, TOOLS_LIST_GROWTH_SINCE_RECORDING),
        client.session(second, tokens["access_token"], TOOLS_LIST, TOOLS_LIST_GROWTH_SINCE_RECORDING))

    # A third session posts the answer. The recording predates get_prompt, and discord_reply doesn't need an
    # earlier fetch (DESIGN.md, deliver_reply), so the recorded calls still replay.
    await client.session(rec[15:18], tokens["access_token"], tools_call(reply_token))
    [(kind, _, body)] = h.discord.calls
    assert kind == "edit" and body["content"].endswith("four")


async def test_an_unprompted_refresh_after_expiry_and_a_one_session_answer_replay_as_recorded(boot):
    h = await boot(login=True, claimed_by=USER)
    rec = recording()
    client = RecordedClient(h)
    tokens = await connect(h, client, rec)
    await set_webhook(h)

    # grok.com refreshed about 32 minutes after the access token had expired (README). By then the sweep has deleted
    # the expired access row, so the refresh starts from no access token at all.
    h.write("DELETE FROM oauth_tokens WHERE kind = 'access'")
    # Not in the recording (grok.com refreshed first), but what it would get with the old token: 401 invalid_token
    # (DESIGN.md, Platform facts: refresh timing).
    stale = await client.mcp(rec[20], {"jsonrpc": "2.0", "id": 0, "method": "initialize", "params": INIT},
                             tokens["access_token"])
    assert stale.status == 401 and 'error="invalid_token"' in stale.headers["WWW-Authenticate"]
    refresh = rec[18]
    refreshed = await h.call("POST", "/oauth/token", headers=replayed_headers(refresh), data={
        "grant_type": "refresh_token", "refresh_token": tokens["refresh_token"], "client_id": "outer-steppes"})
    expect(refresh, refreshed)

    reply_token = await ask(h, rec[19], 1)
    await client.session(rec[20:23], refreshed.json()["access_token"], tools_call(reply_token))
    [(kind, _, body)] = h.discord.calls
    assert kind == "edit" and body["content"].endswith("four")
