"""Environment parsing. Fails closed: any bad or forbidden setting stops startup.

Error messages name the variable but never echo its value.
"""

from __future__ import annotations

import ipaddress
import os
import re
import stat
from collections.abc import Mapping
from dataclasses import dataclass
from urllib.parse import urlsplit

DEFAULT_PUBLIC_BIND = "127.0.0.1:8080"
DEFAULT_ADMIN_BIND = "127.0.0.1:8081"
MAX_SIGNATURE_SKEW_SECONDS = 900  # Discord's interaction token lifetime

# MEMBERSHIP (DESIGN.md, Members and automations; ADR-0016). Who may join, and whether members
# set their own automation. personal and shared both start unclaimed: the first member comes
# from the claim link or the operator.
PERSONAL, SHARED, OPERATOR = "personal", "shared", "operator"
MEMBERSHIP_MODES = (PERSONAL, SHARED, OPERATOR)

# Present at any value, even empty, these stop startup (DESIGN.md, Config).
FORBIDDEN_VARS = ("DISCORD_BOT_TOKEN", "LOG_PROMPTS")


class ConfigError(Exception):
    """Startup configuration is missing, malformed, or forbidden."""


@dataclass(frozen=True)
class Bind:
    host: str
    port: int


@dataclass(frozen=True)
class Config:
    discord_public_key: bytes
    discord_application_id: str
    database_path: str
    public_bind: Bind
    admin_bind: Bind
    signature_max_skew_seconds: int = 300
    guild_allowlist: frozenset[str] = frozenset()
    channel_allowlist: frozenset[str] = frozenset()
    user_allowlist: frozenset[str] = frozenset()
    per_caller_rpm: int = 5
    per_caller_daily: int = 40
    per_channel_max_asks: int = 3
    per_channel_posted_seconds: int = 90
    max_concurrent_automation_posts: int = 8
    max_queued: int = 32
    queue_wait_seconds: int = 120
    registration_token: str = ""
    per_owner_rpm: int = 5
    per_owner_daily: int = 40
    per_owner_max_outstanding: int = 5
    public_base_url: str = "https://bridge.invalid"
    automation_timeout_seconds: int = 20
    reply_ttl_seconds: int = 840
    reply_max_chars: int = 7000
    oauth_redirect_uris: frozenset[str] = frozenset({"https://grok.com/connectors-oauth-exchange-code/"})
    mcp_access_token_ttl_seconds: int = 3_600
    mcp_refresh_token_ttl_seconds: int = 180 * 86_400
    # Discord login for the connector (PR 8a). Empty secret: login is off and the
    # public client does not exist; per-registration clients still work.
    discord_client_secret: str = ""
    oauth_public_client_id: str = "outer-steppes"
    oauth_public_client_secret: str = ""
    oauth_max_pending: int = 500
    mcp_max_connections_per_registration: int = 5
    # Members and automations: MEMBERSHIP, and the cap on members who join a shared
    # bridge by signing in (the claim and the operator aren't capped).
    membership: str = OPERATOR
    max_members: int = 100
    # Members unused this long are made to leave by the retention sweep; 0 never (PR 8d-1).
    member_idle_days: int = 180

    @property
    def discord_login_enabled(self) -> bool:
        return bool(self.discord_client_secret)

    @property
    def members_set_automations(self) -> bool:
        """Members set their own automation on /account, and the bridge can be claimed."""
        return self.membership != OPERATOR


def load_config(env: Mapping[str, str]) -> Config:
    for name in FORBIDDEN_VARS:
        if name in env:
            raise ConfigError(f"{name} must not be set in the server environment")

    public_bind = parse_bind("PUBLIC_BIND", env.get("PUBLIC_BIND", DEFAULT_PUBLIC_BIND), loopback_only=False)
    admin_bind = parse_bind("ADMIN_BIND", env.get("ADMIN_BIND", DEFAULT_ADMIN_BIND), loopback_only=True)
    if public_bind == admin_bind:
        raise ConfigError("PUBLIC_BIND and ADMIN_BIND must differ")

    # Optional since user installs: registration is the gate. When set, it limits
    # which servers that installed the app may use it.
    guilds = _parse_snowflakes("GUILD_ALLOWLIST", env.get("GUILD_ALLOWLIST", ""))

    # personal and shared need Discord login (the claim and every join sign in with it), so the
    # default follows whether login is on, and asking for either without login is refused.
    discord_client_secret = _load_discord_client_secret(env)
    membership = env.get("MEMBERSHIP", PERSONAL if discord_client_secret else OPERATOR).strip().lower()
    if membership not in MEMBERSHIP_MODES:
        raise ConfigError("MEMBERSHIP must be personal, shared or operator")
    if membership != OPERATOR and not discord_client_secret:
        raise ConfigError(f"MEMBERSHIP={membership} needs Discord login: set DISCORD_CLIENT_SECRET or "
                          "DISCORD_CLIENT_SECRET_FILE, or set MEMBERSHIP=operator")

    return Config(
        discord_public_key=_parse_public_key(env.get("DISCORD_PUBLIC_KEY")),
        discord_application_id=_parse_snowflake("DISCORD_APPLICATION_ID", _required(env, "DISCORD_APPLICATION_ID")),
        database_path=_required(env, "DATABASE_PATH"),
        public_bind=public_bind,
        admin_bind=admin_bind,
        signature_max_skew_seconds=_parse_int(env, "SIGNATURE_MAX_SKEW_SECONDS", 300, 1, MAX_SIGNATURE_SKEW_SECONDS),
        guild_allowlist=guilds,
        channel_allowlist=_parse_snowflakes("CHANNEL_ALLOWLIST", env.get("CHANNEL_ALLOWLIST", "")),
        user_allowlist=_parse_snowflakes("USER_ALLOWLIST", env.get("USER_ALLOWLIST", "")),
        per_caller_rpm=_parse_int(env, "PER_CALLER_RPM", 5, 1, 1000),
        per_caller_daily=_parse_int(env, "PER_CALLER_DAILY", 40, 1, 100_000),
        per_channel_max_asks=_parse_int(env, "PER_CHANNEL_MAX_ASKS", 3, 1, 40),
        per_channel_posted_seconds=_parse_int(env, "PER_CHANNEL_POSTED_SECONDS", 90, 0, 840),
        max_concurrent_automation_posts=_parse_int(env, "MAX_CONCURRENT_AUTOMATION_POSTS", 8, 1, 64),
        max_queued=_parse_int(env, "MAX_QUEUED", 32, 0, 1000),
        queue_wait_seconds=_parse_int(env, "QUEUE_WAIT_SECONDS", 120, 10, 300),
        registration_token=_load_registration_token(env),
        per_owner_rpm=_parse_int(env, "PER_OWNER_RPM", 5, 1, 1000),
        per_owner_daily=_parse_int(env, "PER_OWNER_DAILY", 40, 1, 100_000),
        per_owner_max_outstanding=_parse_int(env, "PER_OWNER_MAX_OUTSTANDING", 5, 1, 40),
        public_base_url=_parse_public_base_url(_required(env, "PUBLIC_BASE_URL")),
        automation_timeout_seconds=_parse_int(env, "AUTOMATION_TIMEOUT_SECONDS", 20, 5, 60),
        reply_ttl_seconds=_parse_int(env, "REPLY_TTL_SECONDS", 840, 60, 840),
        reply_max_chars=_parse_int(env, "REPLY_MAX_CHARS", 7000, 1, 7000),
        oauth_redirect_uris=_parse_redirect_uris(env.get("OAUTH_REDIRECT_URIS", DEFAULT_OAUTH_REDIRECT_URI)),
        mcp_access_token_ttl_seconds=_parse_int(env, "MCP_ACCESS_TOKEN_TTL_SECONDS", 3_600, 300, 90 * 86_400),
        mcp_refresh_token_ttl_seconds=_parse_int(env, "MCP_REFRESH_TOKEN_TTL_SECONDS", 180 * 86_400, 3_600,
                                                 365 * 86_400),
        discord_client_secret=discord_client_secret,
        oauth_public_client_id=_parse_public_client_id(env.get("OAUTH_PUBLIC_CLIENT_ID", "outer-steppes")),
        oauth_public_client_secret=_parse_public_client_secret(env.get("OAUTH_PUBLIC_CLIENT_SECRET", "")),
        oauth_max_pending=_parse_int(env, "OAUTH_MAX_PENDING", 500, 10, 10_000),
        mcp_max_connections_per_registration=_parse_int(env, "MCP_MAX_CONNECTIONS_PER_REGISTRATION", 5, 1, 20),
        membership=membership,
        max_members=_parse_int(env, "MAX_MEMBERS", 100, 1, 10_000),
        member_idle_days=_parse_idle_days(env),
    )


_PUBLIC_CLIENT_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def _parse_public_client_id(value: str) -> str:
    """The client id every member types into grok.com. Never shaped like an issued client."""
    value = value.strip()
    if not _PUBLIC_CLIENT_ID.fullmatch(value) or value.startswith("osc_"):
        raise ConfigError("OAUTH_PUBLIC_CLIENT_ID must be 1-64 of A-Z a-z 0-9 - _ and must not start with osc_")
    return value


def _parse_public_client_secret(value: str) -> str:
    """Shared with every member, so not a credential; it only needs to be enterable in grok.com's form."""
    value = value.strip()
    if any(ch.isspace() for ch in value):
        raise ConfigError("OAUTH_PUBLIC_CLIENT_SECRET must not contain whitespace")
    return value


def _load_discord_client_secret(env: Mapping[str, str]) -> str:
    """The application's OAuth2 client secret: at most one of the variable or a 0600 file.

    Refuses a bot token (three dot-separated parts): the server must never hold one,
    and pasting the wrong Developer Portal value into this slot is an easy mistake.
    """
    inline = env.get("DISCORD_CLIENT_SECRET")
    path = env.get("DISCORD_CLIENT_SECRET_FILE")
    if inline is not None and path is not None:
        raise ConfigError("set only one of DISCORD_CLIENT_SECRET and DISCORD_CLIENT_SECRET_FILE")
    if path is not None:
        secret, name = _read_secret_file("DISCORD_CLIENT_SECRET_FILE", path), "DISCORD_CLIENT_SECRET_FILE"
    elif inline is not None and inline.strip():
        secret, name = inline.strip(), "DISCORD_CLIENT_SECRET"
    else:
        return ""  # unset, or an empty `DISCORD_CLIENT_SECRET=` line: login is off
    if not secret or any(ch.isspace() for ch in secret):
        raise ConfigError(f"{name} must be a non-empty value with no whitespace")
    if len(secret.split(".")) == 3:
        raise ConfigError(f"{name} looks like a bot token; use the OAuth2 client secret (Developer Portal > "
                          "OAuth2), never the bot token")
    return secret


# grok.com's OAuth callback for custom connectors (observed by the PR 5 probe).
DEFAULT_OAUTH_REDIRECT_URI = "https://grok.com/connectors-oauth-exchange-code/"


def _parse_redirect_uris(value: str) -> frozenset[str]:
    """Exact callback URLs the OAuth authorize endpoint will redirect to."""
    uris = frozenset(item.strip() for item in value.split(",") if item.strip())
    if not uris:
        raise ConfigError("OAUTH_REDIRECT_URIS must list at least one https URL")
    for uri in uris:
        parts = urlsplit(uri)
        if parts.scheme != "https" or not parts.hostname or parts.fragment:
            raise ConfigError("OAUTH_REDIRECT_URIS entries must be https URLs without fragments")
    return uris


def _parse_public_base_url(value: str) -> str:
    """The https:// origin Funnel serves (ADR-0015)."""
    parts = urlsplit(value)
    if parts.scheme != "https" or not parts.hostname:
        raise ConfigError("PUBLIC_BASE_URL must be an https:// URL")
    if parts.query or parts.fragment or parts.username or parts.password:
        raise ConfigError("PUBLIC_BASE_URL must not have userinfo, a query, or a fragment")
    return value.rstrip("/")


MIN_REGISTRATION_TOKEN_CHARS = 32


def _load_registration_token(env: Mapping[str, str]) -> str:
    """The admin listener's bearer: exactly one of the variable or a 0600 file."""
    inline = env.get("BRIDGE_REGISTRATION_TOKEN")
    path = env.get("BRIDGE_REGISTRATION_TOKEN_FILE")
    if inline is not None and path is not None:
        raise ConfigError("set only one of BRIDGE_REGISTRATION_TOKEN and BRIDGE_REGISTRATION_TOKEN_FILE")
    if path is not None:
        token, name = _read_secret_file("BRIDGE_REGISTRATION_TOKEN_FILE", path), "BRIDGE_REGISTRATION_TOKEN_FILE"
    elif inline is not None:
        token, name = inline.strip(), "BRIDGE_REGISTRATION_TOKEN"
    else:
        raise ConfigError("BRIDGE_REGISTRATION_TOKEN or BRIDGE_REGISTRATION_TOKEN_FILE is required")
    if len(token) < MIN_REGISTRATION_TOKEN_CHARS or any(ch.isspace() for ch in token):
        raise ConfigError(f"{name} must be at least {MIN_REGISTRATION_TOKEN_CHARS} characters with no whitespace")
    return token


def _read_secret_file(name: str, path: str) -> str:
    try:
        if os.name == "posix" and stat.S_IMODE(os.stat(path).st_mode) & 0o077:
            raise ConfigError(f"{name} must not be readable by group or others (use 0600)")
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    except OSError:
        raise ConfigError(f"{name} cannot be read") from None


def _required(env: Mapping[str, str], name: str) -> str:
    value = env.get(name)
    if value is None or not value.strip():
        raise ConfigError(f"{name} is required")
    return value.strip()


def _parse_public_key(value: str | None) -> bytes:
    if not value:
        raise ConfigError("DISCORD_PUBLIC_KEY is required")
    try:
        key = bytes.fromhex(value.strip())
    except ValueError:
        raise ConfigError("DISCORD_PUBLIC_KEY must be hex") from None
    if len(key) != 32:
        raise ConfigError("DISCORD_PUBLIC_KEY must be 32 bytes (64 hex characters)")
    return key


def _parse_snowflake(name: str, value: str) -> str:
    if not value.isascii() or not value.isdigit():
        raise ConfigError(f"{name} must be a Discord id (digits only)")
    return value


def _parse_snowflakes(name: str, value: str) -> frozenset[str]:
    items = [item.strip() for item in value.split(",") if item.strip()]
    return frozenset(_parse_snowflake(name, item) for item in items)


def _parse_int(env: Mapping[str, str], name: str, default: int, low: int, high: int) -> int:
    value = env.get(name)
    if value is None:
        return default
    if not value.isascii() or not value.isdigit():
        raise ConfigError(f"{name} must be a non-negative integer")
    number = int(value)
    if not low <= number <= high:
        raise ConfigError(f"{name} must be between {low} and {high}")
    return number


def _parse_idle_days(env: Mapping[str, str]) -> int:
    """0 (never) or 7-3650. 1-6 are refused: a typo must not make members leave within days."""
    days = _parse_int(env, "MEMBER_IDLE_DAYS", 180, 0, 3650)
    if 0 < days < 7:
        raise ConfigError("MEMBER_IDLE_DAYS must be 0 (never) or between 7 and 3650")
    return days


def parse_bind(name: str, value: str, loopback_only: bool) -> Bind:
    """Parse `host:port` or `[v6host]:port` with an IP literal host.

    Hostnames such as `localhost` are rejected: what they resolve to is not
    under the bridge's control, and the bind is a security boundary.
    """
    if value.startswith("["):
        host, sep, rest = value[1:].partition("]")
        if not sep or not rest.startswith(":"):
            raise ConfigError(f"{name} must be host:port or [ipv6]:port")
        port_text = rest[1:]
    else:
        host, sep, port_text = value.rpartition(":")
        if not sep:
            raise ConfigError(f"{name} must be host:port or [ipv6]:port")
        if ":" in host:
            # "::1:8080" is itself a valid IPv6 address, so an unbracketed
            # v6 host is ambiguous. Require the bracketed form.
            raise ConfigError(f"{name} must write an IPv6 host as [addr]:port")

    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        raise ConfigError(f"{name} host must be an IP address") from None
    if loopback_only and not address.is_loopback:
        raise ConfigError(f"{name} host must be a loopback IP address")

    if not port_text.isascii() or not port_text.isdigit():
        raise ConfigError(f"{name} port must be an integer")
    port = int(port_text)
    if not 1 <= port <= 65535:
        raise ConfigError(f"{name} port must be between 1 and 65535")
    return Bind(str(address), port)
