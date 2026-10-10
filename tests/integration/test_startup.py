"""The real process: what it refuses to start with. `load_config` tests can't prove the process exits."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
VALID = {
    "DISCORD_PUBLIC_KEY": "11" * 32,
    "DISCORD_APPLICATION_ID": "444444444444444444",
    "GUILD_ALLOWLIST": "111111111111111111",
    "BRIDGE_REGISTRATION_TOKEN": "k" * 32,
    "PUBLIC_BASE_URL": "https://bridge.example",
}
# Only what Python needs to start; nothing of the developer's own settings leaks into the process.
INHERITED = ("PATH", "SYSTEMROOT", "TEMP", "TMP", "HOME", "USERPROFILE", "LANG", "LC_ALL")


def run_main(tmp_path, **extra) -> subprocess.CompletedProcess:
    env = {k: os.environ[k] for k in INHERITED if k in os.environ}
    env.update(VALID, DATABASE_PATH=str(tmp_path / "state" / "bridge.db"), PYTHONPATH=str(REPO / "src"), **extra)
    return subprocess.run([sys.executable, "-m", "outer_steppes.main"], env=env, cwd=tmp_path,
                          capture_output=True, text=True, timeout=30)


@pytest.mark.parametrize("name,value", [
    ("MEMBER_IDLE_DAYS", "3"), ("MEMBER_IDLE_DAYS", "abc"), ("MEMBER_IDLE_DAYS", "4000"),
    ("DISCORD_BOT_TOKEN", "made-up-bot-token-123"), ("LOG_PROMPTS", "1"),
])
def test_the_process_refuses_to_start_and_says_why(tmp_path, name, value):
    done = run_main(tmp_path, **{name: value})
    assert done.returncode == 2, done.stderr  # a refused start, not a crash (which would be 1 with a traceback)
    assert name in done.stderr and "Traceback" not in done.stderr
    if name == "DISCORD_BOT_TOKEN":
        assert value not in done.stderr + done.stdout  # a refused secret is never echoed
    assert not (tmp_path / "state").exists()  # it refused before touching the database
