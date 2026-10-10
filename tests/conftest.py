from __future__ import annotations

import binascii
import hashlib
import html
import itertools
import os
import re
from dataclasses import replace

import pytest
from nacl.signing import SigningKey

from outer_steppes import db, foreign
from outer_steppes.config import Bind, Config
from outer_steppes.discord_out import Outcome, Result
from outer_steppes.sweeper import DISCORD_EPOCH_MS

NOW = 1_700_000_000
GUILD = "111111111111111111"
CHANNEL = "222222222222222222"
CALLER = "333333333333333333"
APP_ID = "444444444444444444"
ADMIN_TOKEN = "t" * 40
# A made-up trigger id in the grok provider format; not a real automation.
AUTOMATION_URL = "https://grok.com/webhook/automation/00000000-0000-4000-8000-000000000001"
SECRET = "whsec_MDEyMzQ1Njc4OWFiY2RlZg=="  # the DESIGN.md test vector

_sequence = itertools.count(1)


@pytest.fixture(autouse=True, scope="session")
def production_umask():
    """Run with umask 077, as main.py and the container entrypoint do.

    On POSIX, files the tests create would otherwise be world-readable, and the
    bridge's own 0600/0700 checks would (correctly) reject them. Windows has no
    umask semantics for these checks, which is why this only showed up in CI.
    """
    previous = os.umask(0o077)
    yield
    os.umask(previous)


def snowflake(at: float = NOW) -> str:
    """A unique Discord id whose embedded timestamp is `at`."""
    return str(((int(at * 1000) - DISCORD_EPOCH_MS) << 22) | next(_sequence))


class Signer:
    """Signs bodies the way Discord does: Ed25519 over timestamp + raw body."""

    def __init__(self) -> None:
        self._key = SigningKey.generate()
        self.public_key: bytes = bytes(self._key.verify_key)

    def signature(self, timestamp: str, body: bytes) -> str:
        return self._key.sign(timestamp.encode("ascii") + body).signature.hex()

    def headers(self, body: bytes, timestamp: int = NOW) -> dict[str, str]:
        ts = str(timestamp)
        return {
            "X-Signature-Ed25519": self.signature(ts, body),
            "X-Signature-Timestamp": ts,
            "Content-Type": "application/json",
        }


class FakeDiscord:
    """Records edit_original calls and answers from a scripted list of outcomes."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []                 # edit_original calls
        self.followups: list[tuple[str, str, bool]] = []       # create_followup calls
        self.outcomes: list[Outcome] = []                      # shared, in call order

    def _next(self) -> Result:
        return Result(self.outcomes.pop(0) if self.outcomes else Outcome.OK)

    async def edit_original(self, token: str, content: str) -> Result:
        self.calls.append((token, content))
        return self._next()

    async def create_followup(self, token: str, content: str, ephemeral: bool) -> Result:
        self.followups.append((token, content, ephemeral))
        return self._next()


def assert_token_invariant(conn) -> None:
    """DESIGN.md state machine: a token exists exactly while the row is live or has a pending final."""
    for row in conn.execute("SELECT interaction_id, status, interaction_token, final_message FROM interactions"):
        live = row["status"] in ("queued", "posted", "answering") or row["final_message"] is not None
        has_token = row["interaction_token"] is not None
        if row["status"] in ("done", "claimed"):
            continue  # never had a token
        assert has_token == live, dict(row)


class Clock:
    def __init__(self, now: float = NOW) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def make_config(public_key: bytes = b"\x11" * 32, database_path: str = "unused.db", **overrides) -> Config:
    config = Config(
        discord_public_key=public_key,
        discord_application_id=APP_ID,
        database_path=database_path,
        public_bind=Bind("127.0.0.1", 8080),
        admin_bind=Bind("127.0.0.1", 8081),
        guild_allowlist=frozenset({GUILD}),
        registration_token=ADMIN_TOKEN,
        public_base_url="https://bridge.example",
    )
    return replace(config, **overrides)


def command(name: str = "ask", *, interaction_id: str | None = None, prompt: str | None = "hello",
            private: bool | None = None, caller: str = CALLER, guild: str = GUILD,
            channel: dict | None = None,
            sub: str | None = None, confirm: bool | None = None) -> dict:
    options = []
    if name == "ask" and prompt is not None:
        options.append({"name": "prompt", "type": 3, "value": prompt})
    if private is not None:
        options.append({"name": "private", "type": 5, "value": private})
    if sub is not None:  # /webhook set|status|remove: Discord sends the subcommand as a type-1 option
        options.append({"name": sub, "type": 1, "options": []})
    if confirm is not None:
        options.append({"name": "confirm", "type": 5, "value": confirm})
    return {
        "id": interaction_id or snowflake(),
        "type": 2,
        "token": "interaction-token-abc",
        "application_id": APP_ID,
        "guild_id": guild,
        "channel_id": (channel or {}).get("id", CHANNEL),
        "channel": channel or {"id": CHANNEL, "type": 0},
        "member": {"user": {"id": caller}, "roles": []},
        "data": {"name": name, "type": 1, "options": options},
    }


def add_member(conn, user_id: str = CALLER) -> str:
    """Insert an active member with no automation and return its registration id."""
    registration_id = "reg-" + user_id
    conn.execute("INSERT INTO registrations (id, discord_user_id, created_at, updated_at) VALUES (?, ?, ?, ?)",
                 (registration_id, user_id, NOW, NOW))
    return registration_id


# A made-up id for the bridge's owner: a shared bridge takes members who sign in only once it is
# claimed (DESIGN.md, Members and automations), so tests of joining start from a claimed bridge.
OWNER = "555555555555555555"


def claim_bridge(conn, owner: str = OWNER) -> str:
    """Make the bridge claimed: one active member, the owner, with no automation."""
    return add_member(conn, owner)


def _hash_or_none(secret: str) -> str | None:
    """The stored secret_hash, or none for a made-up secret that isn't valid base64 (never saved for real)."""
    try:
        return foreign.secret_hash(secret)
    except (binascii.Error, ValueError):
        return None


def automation_url_for(user_id: str) -> str:
    """A made-up trigger URL of the member's own: a URL is never stored twice (DESIGN.md, Members and automations).
    The default caller keeps AUTOMATION_URL."""
    if user_id == CALLER:
        return AUTOMATION_URL
    return "https://grok.com/webhook/automation/00000000-0000-4000-8000-" + hashlib.sha256(
        user_id.encode()).hexdigest()[:12]


def register(conn, user_id: str = CALLER, url: str | None = None, secret: str = SECRET) -> str:
    """Insert an active member with an automation and return its registration id.

    The automation reuses the member's id, so tests can name either one. Its URL defaults to the member's own.
    """
    registration_id = add_member(conn, user_id)
    conn.execute(
        "INSERT INTO automations (id, registration_id, kind, automation_url, signing_secret, secret_hash, created_at) "
        "VALUES (?, ?, 'grok_webhook', ?, ?, ?, ?)",
        (registration_id, registration_id, url or automation_url_for(user_id), secret, _hash_or_none(secret), NOW))
    return registration_id


def connect_member(conn, user_id: str = CALLER, expires_at: int = NOW + 365 * 86_400, name: str | None = None) -> None:
    """Give the member a live connector connection: an unexpired refresh token. Answers arrive only through the
    connector, so /ask is refused without one (ADR-0017)."""
    registration_id = "reg-" + user_id
    conn.execute("INSERT INTO oauth_tokens (token_hash, kind, client_id, registration_id, connection_id, "
                 "connected_at, expires_at, created_at) VALUES (?, 'refresh', 'outer-steppes', ?, ?, ?, ?, ?)",
                 (name or "refresh-" + user_id, registration_id, "conn-" + user_id, NOW, expires_at, NOW))


def link_automations(conn) -> None:
    """Record, on hand-built in-flight rows, the automation they were accepted for.

    The ACK path does this for real /asks. register() gives the automation the
    member's id, so the automation is named by the row's registration_id.
    """
    conn.execute("UPDATE interactions SET automation_id = registration_id "
                 "WHERE automation_id IS NULL AND registration_id IS NOT NULL")


async def public_resolver(host: str) -> list[str]:
    return ["93.184.215.14", "2606:2800:21f:cb07:6820:80da:af6b:8b2c"]


@pytest.fixture
def signer() -> Signer:
    return Signer()


@pytest.fixture
def db_path(tmp_path) -> str:
    path = str(tmp_path / "state" / "bridge.db")
    db.initialize(path)
    return path


@pytest.fixture
def conn(db_path):
    connection = db.connect(db_path, db.BACKGROUND_BUSY_TIMEOUT_MS)
    yield connection
    connection.close()


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def fake_discord() -> FakeDiscord:
    return FakeDiscord()


def page_link(text: str, label: str) -> str:
    """The href of a bridge page's link with this text, unescaped, as a browser would follow it."""
    found = re.findall(r"<a[^>]*href='([^']*)'[^>]*>" + re.escape(label) + "</a>", text)
    assert len(found) == 1, (label, text)
    return html.unescape(found[0])
