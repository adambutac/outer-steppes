"""What lies outside the system, faked over real HTTP (DESIGN.md, Testing).

Only Discord's API and the grok.com automation are faked. Everything else an integration
test touches is the real service.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import json
from collections.abc import Callable
from dataclasses import replace
from urllib.parse import urlsplit

import aiohttp
from aiohttp import web

from outer_steppes.automation_client import AutomationClient, PostOutcome
from outer_steppes.url_policy import AcceptedUrl


def standard_webhooks_signature(secret: str, webhook_id: str, timestamp: str, body: bytes) -> str:
    """The receiver's side of Standard Webhooks, written out here on purpose: if it imported the
    bridge's own sign_body, a mistake in that function would be made on both sides and never seen."""
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{webhook_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


class _LocalServer:
    """An aiohttp app on an ephemeral loopback port."""

    def __init__(self) -> None:
        self.app = web.Application()
        self._runner: web.AppRunner | None = None
        self.base = ""

    async def start(self) -> None:
        self._runner = web.AppRunner(self.app, access_log=None)
        await self._runner.setup()
        await web.TCPSite(self._runner, "127.0.0.1", 0).start()
        self.base = f"http://127.0.0.1:{self._runner.addresses[0][1]}"

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()


class FakeDiscordApi(_LocalServer):
    """Discord's webhook endpoints, reached through `DiscordClient(api_base=...)`.

    `calls` holds (kind, interaction token, JSON body) in arrival order, where kind is
    "edit" (the deferred reply's PATCH) or "followup" (a POST).
    """

    def __init__(self) -> None:
        super().__init__()
        self.calls: list[tuple[str, str, dict]] = []
        self.logins: dict[str, tuple[str, str]] = {}   # login code -> (Discord user id, username)
        self.token_requests: list[dict] = []           # the form of each POST /oauth2/token
        self.app.router.add_patch("/webhooks/{app}/{token}/messages/@original", self._edit)
        self.app.router.add_post("/webhooks/{app}/{token}", self._followup)
        self.app.router.add_post("/oauth2/token", self._oauth_token)
        self.app.router.add_get("/users/@me", self._me)

    def login_code(self, user_id: str, username: str = "member") -> str:
        """A one-time code that, exchanged by the bridge, identifies this Discord user."""
        code = f"discord-code-{len(self.logins) + 1}"
        self.logins[code] = (user_id, username)
        return code

    async def _oauth_token(self, request: web.Request) -> web.Response:
        form = dict(await request.post())
        self.token_requests.append(form)
        if form.get("code") not in self.logins:
            return web.json_response({"error": "invalid_grant"}, status=400)
        return web.json_response({"access_token": "discord-at-" + form["code"], "token_type": "Bearer"})

    async def _me(self, request: web.Request) -> web.Response:
        who = self.logins.get(request.headers.get("Authorization", "").removeprefix("Bearer discord-at-"))
        if who is None:
            return web.json_response({"message": "401: Unauthorized"}, status=401)
        return web.json_response({"id": who[0], "username": who[1]})

    async def _edit(self, request: web.Request) -> web.Response:
        self.calls.append(("edit", request.match_info["token"], await request.json()))
        return web.json_response({})

    async def _followup(self, request: web.Request) -> web.Response:
        self.calls.append(("followup", request.match_info["token"], await request.json()))
        return web.json_response({})


class FakeAutomation(_LocalServer):
    """A grok.com automation: checks the bridge's signature, then optionally runs the instructions.

    The signature is verified the way a real receiver does it (Standard Webhooks, with the
    stored secret), so a signing mistake in the bridge shows up here as a 401 and an entry in
    `rejected`. `posts` holds the accepted payloads.

    Like a run in a grok.com account, it reaches the bridge only through that account's Outer
    Steppes connector: `access_token` is the connector's token, or None for an account without
    one. With an `answer`, each run fetches the prompt with get_prompt and, if that worked,
    delivers the answer with discord_reply (DESIGN.md, Payload: the run's instructions). `fetches` and
    `replies` hold the tool results, in order.
    """

    def __init__(self, secret: str, answer: str | None = None) -> None:
        super().__init__()
        self._secret = secret
        self.answer = answer
        self.mcp_url = ""  # the bridge's /mcp; set once it is up
        self.access_token: str | None = None
        self.posts: list[dict] = []
        self.rejected: list[str] = []
        self.fetches: list[dict] = []
        self.replies: list[dict] = []
        self._tasks: set[asyncio.Task] = set()
        self.app.router.add_post("/webhook/automation/{trigger}", self._receive)

    async def _receive(self, request: web.Request) -> web.Response:
        body = await request.read()
        expected = standard_webhooks_signature(self._secret, request.headers.get("webhook-id", ""),
                                               request.headers.get("webhook-timestamp", ""), body)
        if expected not in request.headers.get("webhook-signature", "").split():
            self.rejected.append("signature")
            return web.Response(status=401)
        payload = json.loads(body)
        self.posts.append(payload)
        if self.answer is not None:
            task = asyncio.create_task(self._run(payload["reply_token"], self.answer))
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return web.Response(status=202)

    async def _run(self, reply_token: str, answer: str) -> None:
        fetched = await self.get_prompt(reply_token)
        self.fetches.append(fetched)
        if not fetched.get("isError"):
            self.replies.append(await self.send_reply(reply_token, answer))

    async def get_prompt(self, reply_token: str) -> dict:
        return await self.call_tool("get_prompt", {"reply_token": reply_token})

    async def send_reply(self, reply_token: str, content: str) -> dict:
        return await self.call_tool("discord_reply", {"reply_token": reply_token, "content": content})

    async def call_tool(self, name: str, arguments: dict) -> dict:
        """A tools/call through the connector. Returns the tool result, or {"status": …} if the HTTP call failed
        (with no connector, the bridge answers 401)."""
        headers = {"Authorization": f"Bearer {self.access_token}"} if self.access_token else {}
        message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                   "params": {"name": name, "arguments": arguments}}
        async with aiohttp.ClientSession() as session:
            async with session.post(self.mcp_url, json=message, headers=headers) as resp:
                if resp.status != 200:
                    return {"status": resp.status, "isError": True}
                return (await resp.json())["result"]

    @staticmethod
    def text(result: dict) -> str:
        return result["content"][0]["text"]


class LocalPoster:
    """The real AutomationClient (TLS off), aimed at the fake automation instead of grok.com.

    Signing, classification and timeouts are the production code. The URL policy has already
    accepted the stored grok.com URL by the time this runs.

    `gate` holds sends before they start, as a slow network to grok.com would: a test clears it to keep a POST
    in flight (its row `posted`, its worker holding a slot) for as long as it needs. Set, sends go at once.
    """

    def __init__(self, target_base: Callable[[], str]) -> None:
        self._target_base = target_base
        self._client = AutomationClient(timeout_seconds=5, ssl=False)
        self.gate = asyncio.Event()
        self.gate.set()
        self.held = 0  # sends waiting at the gate

    async def post(self, accepted: AcceptedUrl, body: bytes, secret: str) -> PostOutcome:
        if not self.gate.is_set():
            self.held += 1
            try:
                await self.gate.wait()
            finally:
                self.held -= 1
        local = replace(accepted, url=self._target_base() + urlsplit(accepted.url).path,
                        host="127.0.0.1", addresses=("127.0.0.1",))
        return await self._client.post(local, body, secret)
