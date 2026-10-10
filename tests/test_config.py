from __future__ import annotations

import pytest

from outer_steppes.config import Bind, ConfigError, load_config

KEY_HEX = "11" * 32


def env(**overrides: str) -> dict[str, str]:
    base = {
        "DISCORD_PUBLIC_KEY": KEY_HEX,
        "DISCORD_APPLICATION_ID": "444444444444444444",
        "DATABASE_PATH": "/var/lib/outer-steppes/bridge.db",
        "GUILD_ALLOWLIST": "111111111111111111",
        "BRIDGE_REGISTRATION_TOKEN": "k" * 32,
        "PUBLIC_BASE_URL": "https://bridge.example",
    }
    base.update(overrides)
    return {k: v for k, v in base.items() if v is not None}


def test_defaults():
    config = load_config(env())
    assert config.discord_public_key == bytes.fromhex(KEY_HEX)
    assert config.public_bind == Bind("127.0.0.1", 8080)
    assert config.admin_bind == Bind("127.0.0.1", 8081)
    assert config.signature_max_skew_seconds == 300
    assert config.guild_allowlist == {"111111111111111111"}
    assert config.channel_allowlist == frozenset() and config.user_allowlist == frozenset()
    assert (config.per_caller_rpm, config.per_caller_daily) == (5, 40)
    assert (config.per_channel_max_asks, config.per_channel_posted_seconds) == (3, 90)
    assert (config.max_concurrent_automation_posts, config.max_queued) == (8, 32)


@pytest.mark.parametrize("name", ["DISCORD_BOT_TOKEN", "LOG_PROMPTS"])
@pytest.mark.parametrize("value", ["", "0", "secret-value-123"])
def test_forbidden_variables_stop_startup_at_any_value(name, value):
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(**{name: value}))
    assert name in str(excinfo.value)
    if value:
        assert value not in str(excinfo.value)


@pytest.mark.parametrize("name", ["DISCORD_PUBLIC_KEY", "DISCORD_APPLICATION_ID", "DATABASE_PATH",
                                  "BRIDGE_REGISTRATION_TOKEN"])
def test_required_variables(name):
    with pytest.raises(ConfigError, match=name):
        load_config(env(**{name: None}))


@pytest.mark.parametrize("value", ["not-hex", "11" * 31, "11" * 33])
def test_bad_public_key(value):
    with pytest.raises(ConfigError, match="DISCORD_PUBLIC_KEY"):
        load_config(env(DISCORD_PUBLIC_KEY=value))


@pytest.mark.parametrize("name", ["GUILD_ALLOWLIST", "CHANNEL_ALLOWLIST", "USER_ALLOWLIST"])
def test_allowlists_must_be_snowflakes(name):
    with pytest.raises(ConfigError, match=name):
        load_config(env(**{name: "111,abc"}))


def test_allowlists_parse_and_trim():
    config = load_config(env(GUILD_ALLOWLIST=" 1, 2 ,,3 ", CHANNEL_ALLOWLIST="9", USER_ALLOWLIST=""))
    assert config.guild_allowlist == {"1", "2", "3"}
    assert config.channel_allowlist == {"9"}
    assert config.user_allowlist == frozenset()


def test_guild_allowlist_is_optional_since_user_installs():
    assert load_config(env(GUILD_ALLOWLIST=None)).guild_allowlist == frozenset()
    assert load_config(env(GUILD_ALLOWLIST=" , ")).guild_allowlist == frozenset()


@pytest.mark.parametrize("bind,expected", [
    ("127.0.0.1:8080", Bind("127.0.0.1", 8080)),
    ("0.0.0.0:8080", Bind("0.0.0.0", 8080)),
    ("192.168.1.5:9000", Bind("192.168.1.5", 9000)),
    ("[::]:8080", Bind("::", 8080)),
])
def test_public_bind_accepts_any_ip_literal(bind, expected):
    assert load_config(env(PUBLIC_BIND=bind)).public_bind == expected


@pytest.mark.parametrize("bind,expected", [
    ("127.0.0.1:9001", Bind("127.0.0.1", 9001)),
    ("127.0.0.2:9001", Bind("127.0.0.2", 9001)),
    ("[::1]:9001", Bind("::1", 9001)),
])
def test_admin_bind_accepts_loopback(bind, expected):
    assert load_config(env(ADMIN_BIND=bind)).admin_bind == expected


@pytest.mark.parametrize("bind", ["0.0.0.0:8081", "192.168.1.5:8081", "[::]:8081", "[2001:db8::1]:8081"])
def test_admin_bind_rejects_non_loopback(bind):
    with pytest.raises(ConfigError, match="ADMIN_BIND host must be a loopback"):
        load_config(env(ADMIN_BIND=bind))


@pytest.mark.parametrize("name", ["PUBLIC_BIND", "ADMIN_BIND"])
@pytest.mark.parametrize("bind", [
    "localhost:8080", "example.com:8080", "127.0.0.1", "127.0.0.1:", "127.0.0.1:0",
    "127.0.0.1:70000", "127.0.0.1:80a", "[::1]8080", "::1:8080",
])
def test_malformed_binds_are_rejected(name, bind):
    with pytest.raises(ConfigError, match=name):
        load_config(env(**{name: bind}))


def test_binds_must_differ():
    with pytest.raises(ConfigError, match="must differ"):
        load_config(env(PUBLIC_BIND="127.0.0.1:9000", ADMIN_BIND="127.0.0.1:9000"))


@pytest.mark.parametrize("name,value", [
    ("SIGNATURE_MAX_SKEW_SECONDS", "0"), ("SIGNATURE_MAX_SKEW_SECONDS", "901"),
    ("PER_CHANNEL_POSTED_SECONDS", "841"),
    ("PER_CHANNEL_MAX_ASKS", "0"), ("PER_CHANNEL_MAX_ASKS", "41"), ("QUEUE_WAIT_SECONDS", "5"),
    ("PER_CALLER_RPM", "0"), ("MAX_CONCURRENT_AUTOMATION_POSTS", "abc"), ("MAX_QUEUED", "-1"),
])
def test_numeric_ranges(name, value):
    with pytest.raises(ConfigError, match=name):
        load_config(env(**{name: value}))


# ------------------------------------------------------------------ PR 3

def test_registration_token_minimum_length_and_whitespace():
    assert load_config(env(BRIDGE_REGISTRATION_TOKEN="k" * 32)).registration_token == "k" * 32
    for bad in ("k" * 31, "k" * 20 + " " + "k" * 20):
        with pytest.raises(ConfigError, match="at least 32 characters"):
            load_config(env(BRIDGE_REGISTRATION_TOKEN=bad))


def test_registration_token_file(tmp_path):
    path = tmp_path / "token"
    path.write_text("f" * 40 + "\n", encoding="utf-8")
    config = load_config(env(BRIDGE_REGISTRATION_TOKEN=None, BRIDGE_REGISTRATION_TOKEN_FILE=str(path)))
    assert config.registration_token == "f" * 40


def test_registration_token_sources_are_exclusive(tmp_path):
    path = tmp_path / "token"
    path.write_text("f" * 40, encoding="utf-8")
    with pytest.raises(ConfigError, match="only one"):
        load_config(env(BRIDGE_REGISTRATION_TOKEN_FILE=str(path)))


def test_registration_token_never_echoed():
    secret = "s3cr3t" * 3  # too short
    with pytest.raises(ConfigError) as excinfo:
        load_config(env(BRIDGE_REGISTRATION_TOKEN=secret))
    assert secret not in str(excinfo.value)


def test_owner_limits():
    config = load_config(env(PER_OWNER_RPM="3", PER_OWNER_DAILY="9", PER_OWNER_MAX_OUTSTANDING="2"))
    assert (config.per_owner_rpm, config.per_owner_daily, config.per_owner_max_outstanding) == (3, 9, 2)
    with pytest.raises(ConfigError, match="PER_OWNER_MAX_OUTSTANDING"):
        load_config(env(PER_OWNER_MAX_OUTSTANDING="0"))


# ------------------------------------------------------------------ PR 5

def test_oauth_defaults_and_redirect_uris():
    config = load_config(env())
    assert config.oauth_redirect_uris == {"https://grok.com/connectors-oauth-exchange-code/"}
    # PR 8a: grok.com refreshes readily (login probe), so access tokens are short.
    assert (config.mcp_access_token_ttl_seconds, config.mcp_refresh_token_ttl_seconds) == (3_600, 180 * 86_400)
    many = load_config(env(OAUTH_REDIRECT_URIS="https://a.example/cb, https://b.example/cb"))
    assert many.oauth_redirect_uris == {"https://a.example/cb", "https://b.example/cb"}
    for bad in ("http://grok.com/cb", " , ", "https://grok.com/cb#frag"):
        with pytest.raises(ConfigError, match="OAUTH_REDIRECT_URIS"):
            load_config(env(OAUTH_REDIRECT_URIS=bad))


# ------------------------------------------------------------------ PR 8d-1

def test_member_idle_days_defaults_to_180():
    assert load_config(env()).member_idle_days == 180


@pytest.mark.parametrize("value,expected", [("0", 0), ("7", 7), ("365", 365), ("3650", 3650)])
def test_member_idle_days_accepts_zero_or_seven_to_3650(value, expected):
    assert load_config(env(MEMBER_IDLE_DAYS=value)).member_idle_days == expected


@pytest.mark.parametrize("value", ["1", "6", "3651", "-1", "abc", "", "1.5"])
def test_member_idle_days_out_of_range_refuses_to_start(value):
    """1-6 are refused too: a typo must not make members leave within days."""
    with pytest.raises(ConfigError, match="MEMBER_IDLE_DAYS"):
        load_config(env(MEMBER_IDLE_DAYS=value))
