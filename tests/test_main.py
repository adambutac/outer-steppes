from __future__ import annotations

from pathlib import Path

import pytest

from outer_steppes import main as main_module

PACKAGE_DIR = Path(main_module.__file__).parent

GOOD_ENV = {
    "DISCORD_PUBLIC_KEY": "11" * 32,
    "DISCORD_APPLICATION_ID": "444",
    "GUILD_ALLOWLIST": "111",
    "BRIDGE_REGISTRATION_TOKEN": "k" * 32,
    "PUBLIC_BASE_URL": "https://bridge.example",
}
ALL_VARS = ("DISCORD_PUBLIC_KEY", "DISCORD_APPLICATION_ID", "GUILD_ALLOWLIST", "DATABASE_PATH",
            "BRIDGE_REGISTRATION_TOKEN", "BRIDGE_REGISTRATION_TOKEN_FILE", "PUBLIC_BASE_URL",
            "DISCORD_BOT_TOKEN", "LOG_PROMPTS", "PUBLIC_BIND", "ADMIN_BIND")


def set_env(monkeypatch, environ):
    for name in ALL_VARS:
        monkeypatch.delenv(name, raising=False)
    for name, value in environ.items():
        monkeypatch.setenv(name, value)


@pytest.mark.parametrize("extra", [
    {"DATABASE_PATH": None},
    {"DISCORD_BOT_TOKEN": "x"},
    {"ADMIN_BIND": "0.0.0.0:8081"},
])
def test_bad_config_exits_2_without_starting(monkeypatch, capsys, tmp_path, extra):
    environ = {**GOOD_ENV, "DATABASE_PATH": str(tmp_path / "b.db"), **extra}
    set_env(monkeypatch, {k: v for k, v in environ.items() if v is not None})

    async def fail_serve(config):
        raise AssertionError("server must not start with bad config")

    monkeypatch.setattr(main_module, "serve", fail_serve)
    monkeypatch.setattr(main_module.os, "umask", lambda mask: 0)
    assert main_module.main() == 2
    assert capsys.readouterr().err.startswith("outer-steppes: ")


def test_database_error_exits_2(monkeypatch, capsys, tmp_path):
    path = tmp_path / "b.db"
    import sqlite3
    conn = sqlite3.connect(path)
    conn.execute("PRAGMA user_version = 9")
    conn.close()
    set_env(monkeypatch, {**GOOD_ENV, "DATABASE_PATH": str(path)})
    monkeypatch.setattr(main_module.os, "umask", lambda mask: 0)
    assert main_module.main() == 2
    assert "version 9" in capsys.readouterr().err


def test_server_package_never_uses_a_bot_token():
    # DESIGN.md, Identity: the serving process never holds or sends the bot token.
    for path in PACKAGE_DIR.rglob("*.py"):
        source = path.read_text(encoding="utf-8")
        assert "Authorization: Bot" not in source, path
        assert '"Bot ' not in source and "f\"Bot " not in source, path
