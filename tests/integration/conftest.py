"""The integration layer's fixture: the real service on a temp database, with fakes only outside it.

See DESIGN.md, Testing. `boot()` starts what `main.py` starts (through `main.running`), on
ephemeral loopback ports, and returns a Harness that talks to it over real HTTP.
"""

from __future__ import annotations

import asyncio
import contextlib
import inspect
import json
import sqlite3
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path

import aiohttp
import pytest

from outer_steppes import metrics
from outer_steppes.config import Bind, Config
from outer_steppes.main import Stack, running

from ..conftest import (ADMIN_TOKEN, AUTOMATION_URL, CALLER, SECRET, Signer, make_config, public_resolver,
                        snowflake)
from .clients import Connector, Reply
from .fakes import FakeAutomation, FakeDiscordApi, LocalPoster

HERE = Path(__file__).parent


def pytest_collection_modifyitems(items):
    """Everything under tests/integration/ carries the `integration` marker (so `-m` can select it)."""
    for item in items:
        if HERE in Path(str(item.fspath)).parents:
            item.add_marker(pytest.mark.integration)


@pytest.fixture(autouse=True)
def fresh_metrics():
    return metrics.reset()


async def eventually(check: Callable[[], object], what: str, timeout: float = 10.0):
    """Poll `check` until it returns something truthy, and return it. No fixed sleeps: a bounded poll."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        result = check()
        if inspect.isawaitable(result):
            result = await result
        if result:
            return result
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"timed out after {timeout}s waiting for {what}")
        await asyncio.sleep(0.02)


@dataclass
class Harness:
    config: Config
    stack: Stack
    discord: FakeDiscordApi
    automation: FakeAutomation
    poster: LocalPoster
    signer: Signer
    http: aiohttp.ClientSession

    @property
    def public(self) -> str:
        return f"http://127.0.0.1:{self.stack.public_port}"

    @property
    def admin(self) -> str:
        return f"http://127.0.0.1:{self.stack.admin_port}"

    async def interact(self, interaction: dict, *, timestamp: int | None = None, sign: bool = True,
                       fresh_id: bool = True) -> tuple[int, dict]:
        """POST a signed interaction to the real /interactions. Returns (status, JSON body or {}).

        Each call gets a new interaction id stamped with the current time, because the reply
        window is measured from the id's embedded timestamp (Discord's own 15-minute token life);
        the shared helper's ids are dated 2023, which the bridge would rightly treat as expired.
        Pass fresh_id=False to resend the same id (a replay).
        """
        if fresh_id:
            interaction = {**interaction, "id": snowflake(time.time())}
        body = json.dumps(interaction).encode()
        headers = {"Content-Type": "application/json"}
        if sign:
            headers = self.signer.headers(body, timestamp=int(time.time()) if timestamp is None else timestamp)
        async with self.http.post(self.public + "/interactions", data=body, headers=headers) as resp:
            text = await resp.text()
            return resp.status, (json.loads(text) if resp.content_type == "application/json" else {"text": text})

    async def say(self, interaction: dict) -> str:
        """The ephemeral text of the bridge's answer to an interaction."""
        status, body = await self.interact(interaction)
        assert status == 200, body
        return body["data"]["content"]

    async def call(self, method: str, path: str, *, headers: dict | None = None, data: dict | bytes | None = None,
                   json_body: dict | None = None, admin: bool = False) -> Reply:
        """Any request to either listener. Redirects are never followed and cookies are never managed for you:
        a test reads Location and Set-Cookie itself (DESIGN.md, Testing: Secure cookies)."""
        base = self.admin if admin else self.public
        async with self.http.request(method, base + path, headers=headers, data=data, json=json_body,
                                     allow_redirects=False) as resp:
            return Reply(resp.status, resp.headers.copy(), await resp.text())

    async def operator(self, method: str, path: str, *, json_body: dict | None = None,
                       bearer: bool = True) -> tuple[int, object]:
        """A call to the admin listener, as osctl makes it."""
        headers = {"Authorization": f"Bearer {ADMIN_TOKEN}"} if bearer else {}
        async with self.http.request(method, self.admin + path, json=json_body, headers=headers) as resp:
            return resp.status, (await resp.json() if resp.content_type == "application/json" else await resp.text())

    async def register(self, user_id: str = CALLER, url: str = AUTOMATION_URL, secret: str = SECRET) -> None:
        status, body = await self.operator("POST", "/v1/registrations", json_body={
            "discord_user_id": user_id, "automation_url": url, "signing_secret": secret, "label": "test"})
        assert status == 200, body

    async def connect(self, user_id: str = CALLER) -> str:
        """Connect the member's Outer Steppes connector in the automation's grok.com account, with an issued client
        (`osctl connector client add`), so it works with Discord login off too. Returns the access token, which the
        fake automation then uses for get_prompt and discord_reply."""
        status, issued = await self.operator("POST", f"/v1/registrations/{user_id}/mcp-clients")
        assert status == 200, issued
        connector = Connector(self, issued["client_id"], issued["client_secret"])
        tokens = await connector.exchange(await connector.issued_code())
        self.automation.access_token = tokens.access
        return tokens.access

    async def claim(self, user_id: str, username: str = "member") -> Reply:
        """Claim the bridge as its owner does: the link from POST /v1/claim (osctl bridge claim), then Discord.

        Returns the callback's reply, which carries the account-page session cookie."""
        status, minted = await self.operator("POST", "/v1/claim")
        assert status == 200, minted
        link = minted["claim_url"]
        assert link.startswith(self.config.public_base_url + "/account/claim?token=")
        started = await self.call("GET", link[len(self.config.public_base_url):])
        assert started.status == 302, started.text
        code = self.discord.login_code(user_id, username)
        returned = await self.call(
            "GET", f"/oauth/discord/callback?state={started.query()['state']}&code={code}",
            headers={"Cookie": f"__Host-os_login={started.cookie('__Host-os_login')}"})
        assert returned.status == 303, (returned.status, returned.text)
        return returned

    async def members(self) -> list[str]:
        status, body = await self.operator("GET", "/v1/registrations")
        assert status == 200
        return [m["discord_user_id"] for m in body]

    def write(self, sql: str, params: tuple = ()) -> None:
        """One short write to the running service's database (it uses WAL, so this doesn't stop the service).
        For arranging a state a test can't reach through the public surfaces, such as a backdated row (time is
        faked that way)."""
        conn = sqlite3.connect(self.config.database_path, timeout=5)
        try:
            conn.execute(sql, params)
            conn.commit()
        finally:
            conn.close()

    def db(self) -> sqlite3.Connection:
        """A read-only connection to the running service's database file."""
        conn = sqlite3.connect(f"file:{self.config.database_path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn


@pytest.fixture
async def boot(tmp_path):
    """`await boot(...)` starts the service and returns a Harness; everything is torn down afterward,
    including whatever had started before a failing step (that is what the exit stack is for)."""
    cleanup = contextlib.AsyncExitStack()

    async def start(*, member_idle_days: int = 180, sweep_interval: float = 30.0, answer: str | None = None,
                    before_start: Callable[[str], None] | None = None, login: bool = False,
                    membership: str = "personal", claimed_by: str | None = None, **settings) -> Harness:
        """`login=True` turns on the Discord login (the connector and account page), with `membership` as
        MEMBERSHIP, and `claimed_by` claims the bridge through the real claim link before the test starts.
        Any other keyword is a Config field, as its environment variable would set it (`user_allowlist` for
        USER_ALLOWLIST)."""
        path = str(tmp_path / "state" / "bridge.db")
        if before_start is not None:
            before_start(path)  # prepare a database file (for example rows written before boot)
        signer = Signer()
        overrides = {"discord_client_secret": "made-up-discord-client-secret", "membership": membership} if login else {}
        config = make_config(signer.public_key, path, public_bind=Bind("127.0.0.1", 0),
                             admin_bind=Bind("127.0.0.1", 0), member_idle_days=member_idle_days, **overrides,
                             **settings)
        discord, automation = FakeDiscordApi(), FakeAutomation(SECRET, answer)
        await discord.start()
        cleanup.push_async_callback(discord.stop)
        await automation.start()
        cleanup.push_async_callback(automation.stop)
        poster = LocalPoster(lambda: automation.base)
        stack = await cleanup.enter_async_context(
            running(config, discord_api_base=discord.base, poster=poster,
                    resolver=public_resolver, sweep_interval=sweep_interval))
        automation.mcp_url = f"http://127.0.0.1:{stack.public_port}/mcp"
        http = aiohttp.ClientSession(cookie_jar=aiohttp.DummyCookieJar())  # cookies are handled by hand
        cleanup.push_async_callback(http.close)
        harness = Harness(config, stack, discord, automation, poster, signer, http)
        if claimed_by is not None:
            await harness.claim(claimed_by)
        return harness

    yield start
    await cleanup.aclose()
