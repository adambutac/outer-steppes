"""osctl_container.py: loopback-only publish, env file never printed, Funnel only at this machine's name, snapshots outside the repo."""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import os
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "osctl_container.py"
spec = importlib.util.spec_from_file_location("osctl_container", SCRIPT)
dev = importlib.util.module_from_spec(spec)
spec.loader.exec_module(dev)
REAL_REPO = dev.REPO

TOKEN = "made-up-registration-token-0123456789abcdef"
TS_NAME = "made-up-name.tail0000.ts.net"
BASE = f"https://{TS_NAME}"
# What tunnel runs: the public listener only, in the foreground.
FUNNEL = ["tailscale", "funnel", "127.0.0.1:8080"]
# How up asks whether this podman takes --env-host, and what each client answers.
RUN_HELP = ["podman", "run", "--help"]
LOCAL_RUN_HELP = ("      --env-file stringArray   Read in a file of environment variables\n"
                  "      --env-host               Use all current host environment variables in container\n")
REMOTE_RUN_HELP = "      --env-file stringArray   Read in a file of environment variables\n"


class FakeRunner:
    """Records every command; answers `podman inspect` with the given container state,
    and `tailscale status --json` with the given status (None: the command fails)."""

    def __init__(self, state="absent", fail=(), tailscale=None, run_help=LOCAL_RUN_HELP, volume=True, members=1):
        self.calls = []
        self.captured = []  # each call's capture flag, in step with calls
        self.volume_exists = volume
        self.members = members  # in the database db-snapshot and reset copy out
        self.scratch_exists = False  # restore's scratch volume
        self.scratch_labeled = False  # made by restore, so it may be removed as a leftover
        self.container_state = state
        self.fail = fail
        self.tailscale = ts_status() if tailscale is None else tailscale
        self.run_help = run_help

    def __call__(self, cmd, capture=False):
        self.calls.append(cmd)
        self.captured.append(capture)
        if cmd == RUN_HELP:
            return (1, "") if self.run_help is None else (0, self.run_help)
        if cmd[:2] == ["podman", "inspect"]:
            if self.container_state == "absent":
                return 1, ""
            if cmd[5] == "{{.State.Status}}":
                return 0, f"{self.container_state}\n"
            return 0, f"{self.container_state} healthy outer-steppes outer-steppes-data\n"
        if cmd[:3] == ["tailscale", "status", "--json"]:
            return (1, "") if self.tailscale is False else (0, self.tailscale)
        if cmd[:2] == ["git", "rev-parse"]:
            return 0, "abc1234\n"
        if cmd[:2] == ["git", "status"]:
            return 0, ""
        if cmd[:3] == ["podman", "volume", "exists"]:
            if cmd[3].endswith("-restore-check"):
                return (0 if self.scratch_exists else 1), ""
            if self.volume_exists is None:
                return 125, ""  # Podman itself failed
            return (0 if self.volume_exists else 1), ""
        if any(cmd[:len(f)] == list(f) for f in self.fail):
            return 1, ""
        if cmd[:3] in (["podman", "volume", "create"], ["podman", "volume", "rm"]) \
                and cmd[-1].endswith("-restore-check"):
            self.scratch_exists = cmd[2] == "create"
            self.scratch_labeled = cmd[2] == "create" and "--label" in cmd
        if cmd[:3] == ["podman", "volume", "inspect"] and cmd[-1].endswith("-restore-check"):
            return 0, ("1" if self.scratch_labeled else "") + "\n"  # what Podman 5.8 prints
        if cmd[:2] == ["podman", "cp"]:
            made_up_db(Path(cmd[3]), self.members)
        if cmd[:3] == ["podman", "volume", "export"]:
            made_up_tar(Path(cmd[cmd.index("--output") + 1]), self.members)
        if cmd[:2] == ["podman", "rm"]:
            self.container_state = "absent"
        return 0, ""

    def commands(self, *prefix):
        return [c for c in self.calls if c[:len(prefix)] == list(prefix) and c != RUN_HELP]


def made_up_db(path, members=1, extra_table=None):
    """A database with the real schema and `members` rows in registrations."""
    import outer_steppes.db as db
    db.initialize(str(path))
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.executemany("INSERT INTO registrations (id, discord_user_id, created_at, updated_at) VALUES (?, ?, 0, 0)",
                         [(f"made-up-reg-{i}", f"10000000000000000{i}") for i in range(members)])
        if extra_table:
            conn.execute(f"CREATE TABLE {extra_table} (x)")
        conn.commit()
        conn.execute("PRAGMA journal_mode = DELETE")  # one file, as a backup or a stopped volume holds it
    return path


def made_up_tar(path, members=1, arcname="./bridge.db"):
    """A volume export as `podman volume export` writes it: the volume's files under ./."""
    with tempfile.TemporaryDirectory() as tmp:
        made_up_db(Path(tmp) / "bridge.db", members)
        with tarfile.open(path, "w") as tar:
            tar.add(Path(tmp) / "bridge.db", arcname=arcname)
    return path


def ts_status(name=TS_NAME + ".", state="Running"):
    # The real output also lists peers and addresses, which osctl_container.py must ignore.
    return json.dumps({"BackendState": state, "Self": {"DNSName": name, "TailscaleIPs": ["100.64.0.1"]},
                       "Peer": {"made-up-key": {"DNSName": "other.tail0000.ts.net."}}})


@pytest.fixture
def env_file(tmp_path):
    path = tmp_path / "outer-steppes.env"
    path.write_text(
        f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\nPUBLIC_BASE_URL={BASE}\nDISCORD_CLIENT_SECRET=made-up-secret\n",
        encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolated_dirs(tmp_path, monkeypatch):
    """Every test gets its own repo root and cwd, so the real repo's .env is never found."""
    repo, cwd = tmp_path / "repo", tmp_path / "cwd"
    repo.mkdir()
    cwd.mkdir()
    monkeypatch.setattr(dev, "REPO", repo)
    monkeypatch.chdir(cwd)  # relative paths resolve here, away from the real repo
    return repo, cwd


def make(runner, env=None, interactive=True, healthy=True, answer=None):
    out, err = io.StringIO(), io.StringIO()

    def ask(prompt):
        if answer is None:
            pytest.fail("must not ask")
        if answer is EOFError:
            raise EOFError
        return answer
    d = dev.Dev(runner=runner, env=env or {}, out=out, err=err,
                interactive=lambda: interactive, wait_healthy=lambda: healthy, ask=ask)
    return d, out, err


def run(argv, runner, **kw):
    d, out, err = make(runner, **kw)
    return dev.run(argv, d), out.getvalue(), err.getvalue()


def test_up_publishes_on_loopback_only_and_never_pulls(env_file):
    runner = FakeRunner()
    code, _, _ = run(["up", "--env-file", str(env_file)], runner)
    assert code == 0
    [cmd] = runner.commands("podman", "run")
    publishes = [cmd[i + 1] for i, a in enumerate(cmd) if a in ("-p", "--publish")]
    assert publishes == ["127.0.0.1:8080:8080"]
    assert not any("8081" in a for a in cmd)
    assert "-P" not in cmd and "--publish-all" not in cmd and "--network=host" not in cmd
    assert cmd[cmd.index("--pull") + 1] == "never"
    assert cmd[cmd.index("--env-file") + 1] == str(env_file)
    assert cmd[cmd.index("-v") + 1] == "outer-steppes-data:/var/lib/outer-steppes"
    # A containers.conf with env_host = true would otherwise copy this shell, bot token included (ADR-0008).
    assert cmd[-1] == dev.IMAGE and "--env-host=false" in cmd[:-1]  # a run option, not an argument


def test_up_leaves_out_env_host_where_the_client_rejects_it(env_file):
    """Podman 5.8's Windows remote client fails `podman run --env-host=false` with "unknown flag"."""
    runner = FakeRunner(run_help=REMOTE_RUN_HELP)
    assert run(["up", "--env-file", str(env_file)], runner)[0] == 0
    [cmd] = runner.commands("podman", "run")
    assert not any(a.startswith("--env-host") for a in cmd)
    runner = FakeRunner(run_help=None)  # the probe itself fails: no flag, and up still runs
    assert run(["up", "--env-file", str(env_file)], runner)[0] == 0
    [cmd] = runner.commands("podman", "run")
    assert not any(a.startswith("--env-host") for a in cmd)


def test_up_uses_os_volume_and_os_env_file(env_file):
    runner = FakeRunner()
    code, _, _ = run(["up"], runner, env={"OS_ENV_FILE": str(env_file), "OS_VOLUME": "os-smoke-data"})
    assert code == 0
    [cmd] = runner.commands("podman", "run")
    assert cmd[cmd.index("-v") + 1] == "os-smoke-data:/var/lib/outer-steppes"


def test_up_needs_an_existing_env_file(tmp_path):
    runner = FakeRunner()
    code, _, err = run(["up"], runner)
    assert code == 2 and "OS_ENV_FILE" in err and ".env" in err
    code, _, err = run(["up", "--env-file", str(tmp_path / "missing.env")], runner)
    assert code == 2 and "not found" in err
    assert not runner.commands("podman", "run")


def env_run(runner):
    [cmd] = runner.commands("podman", "run")
    return cmd[cmd.index("--env-file") + 1]


def test_up_finds_the_repo_root_dot_env(isolated_dirs):
    repo, _ = isolated_dirs
    (repo / ".env").write_text(f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\n", encoding="utf-8")
    runner = FakeRunner()
    code, out, err = run(["up"], runner)
    assert code == 0 and env_run(runner) == str(repo / ".env")
    assert f"env file: {repo / '.env'}" in err and TOKEN not in out + err


def test_a_dot_env_in_the_cwd_is_never_used(isolated_dirs):
    repo, cwd = isolated_dirs
    (cwd / ".env").write_text("OTHER_PROJECT_SECRET=made-up\n", encoding="utf-8")
    runner = FakeRunner()
    code, _, err = run(["up"], runner)
    assert code == 2 and "repo root" in err and not runner.commands("podman", "run")
    (repo / ".env").write_text("X=1\n", encoding="utf-8")
    runner = FakeRunner()
    run(["up"], runner)
    assert env_run(runner) == str(repo / ".env")


def test_an_explicit_env_file_beats_a_dot_env(isolated_dirs, env_file, tmp_path):
    repo, cwd = isolated_dirs
    (cwd / ".env").write_text("X=1\n", encoding="utf-8")
    runner = FakeRunner()
    run(["up"], runner, env={"OS_ENV_FILE": str(env_file)})
    assert env_run(runner) == str(env_file)
    runner = FakeRunner()
    run(["up", "--env-file", str(env_file)], runner, env={"OS_ENV_FILE": str(cwd / ".env")})
    assert env_run(runner) == str(env_file)
    # A missing explicit path is an error, not a reason to fall back to .env.
    runner = FakeRunner()
    code, _, err = run(["up"], runner, env={"OS_ENV_FILE": str(tmp_path / "missing.env")})
    assert code == 2 and "not found" in err and not runner.commands("podman", "run")


def test_the_search_never_goes_above_the_repo_root(isolated_dirs):
    repo, _ = isolated_dirs
    (repo.parent / ".env").write_text("X=1\n", encoding="utf-8")  # the parent of both dirs
    runner = FakeRunner()
    code, _, _ = run(["up"], runner)
    assert code == 2 and not runner.commands("podman", "run")


def test_tunnel_finds_public_base_url_in_a_dot_env(isolated_dirs):
    repo, _ = isolated_dirs
    (repo / ".env").write_text(f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\nPUBLIC_BASE_URL={BASE}\n", encoding="utf-8")
    runner = FakeRunner()
    code, out, err = run(["tunnel"], runner)
    assert code == 0 and runner.commands("tailscale", "funnel") == [FUNNEL] and TOKEN not in out + err


def test_nothing_from_the_env_file_is_ever_printed(env_file, capsys):
    for argv, state in ((["up"], "absent"), (["up"], "running"), (["restart"], "running"),
                        (["tunnel"], "absent"), (["status"], "running")):
        code, out, err = run(argv, FakeRunner(state), env={"OS_ENV_FILE": str(env_file)})
        real = capsys.readouterr()  # a stray print() bypasses the Dev streams
        seen = out + err + real.out + real.err
        assert TOKEN not in seen and "made-up-secret" not in seen, argv


def test_up_leaves_a_running_container_alone(env_file):
    runner = FakeRunner("running")
    code, out, _ = run(["up", "--env-file", str(env_file)], runner)
    assert code == 0 and "already running" in out
    assert not runner.commands("podman", "run") and not runner.commands("podman", "rm")


def test_up_recreates_a_stopped_container(env_file):
    runner = FakeRunner("exited")
    code, _, _ = run(["up", "--env-file", str(env_file)], runner)
    assert code == 0
    rm, run_ = runner.calls.index(["podman", "rm", "outer-steppes"]), runner.calls.index(
        runner.commands("podman", "run")[0])
    assert rm < run_


def test_up_reports_an_unhealthy_start(env_file):
    code, _, err = run(["up", "--env-file", str(env_file)], FakeRunner(), healthy=False)
    assert code == 1 and "logs" in err


def test_up_build_builds_first_and_stops_if_it_fails(env_file):
    runner = FakeRunner()
    run(["up", "--build", "--env-file", str(env_file)], runner)
    assert runner.calls.index(runner.commands("podman", "build")[0]) < runner.calls.index(
        runner.commands("podman", "run")[0])
    runner = FakeRunner(fail=[("podman", "build")])
    code, _, _ = run(["up", "--build", "--env-file", str(env_file)], runner)
    assert code == 1 and not runner.commands("podman", "run")


def test_build_tags_the_commit():
    runner = FakeRunner()
    run(["build"], runner)
    [cmd] = runner.commands("podman", "build")
    # --format docker keeps the HEALTHCHECK, which Podman's default OCI format drops.
    assert cmd == ["podman", "build", "--format", "docker", "-f", "deploy/Dockerfile", "-t", "outer-steppes",
                   "-t", "outer-steppes:abc1234", "."]


def test_down_keeps_the_volume():
    runner = FakeRunner("running")
    code, _, _ = run(["down"], runner)
    assert code == 0
    assert runner.commands("podman", "stop") == [["podman", "stop", "outer-steppes"]]
    assert runner.commands("podman", "rm") == [["podman", "rm", "outer-steppes"]]
    assert not runner.commands("podman", "volume")
    # Funnel is the tunnel terminal's to stop; down could otherwise end a real bridge's --bg Funnel.
    assert not runner.commands("tailscale")


def test_restart_checks_the_env_file_before_stopping(tmp_path):
    runner = FakeRunner("running")
    code, _, _ = run(["restart", "--env-file", str(tmp_path / "missing.env")], runner)
    assert code == 2 and not runner.commands("podman", "stop")


def test_osctl_forwards_its_arguments_and_names_the_container():
    runner = FakeRunner("running")
    d, _, err = make(runner)
    d.osctl(["member", "add", "123", "--label", "me"])
    assert runner.calls[-1] == ["podman", "exec", "-it", "outer-steppes", "osctl",
                                "member", "add", "123", "--label", "me"]
    assert "running in container outer-steppes" in err.getvalue()
    d, _, _ = make(runner, interactive=False)
    d.osctl(["webhook", "set", "123"])
    assert runner.calls[-1][:3] == ["podman", "exec", "-i"]  # piped stdin, no -t


def test_osctl_is_no_longer_a_subcommand():
    """The osctl API groups reach the container themselves."""
    runner = FakeRunner("running")
    with pytest.raises(SystemExit):
        run(["osctl", "bridge", "status"], runner)
    assert not runner.commands("podman", "exec")


class Tty(io.StringIO):
    def __init__(self, tty):
        super().__init__()
        self.tty = tty

    def isatty(self):
        return self.tty


@pytest.mark.parametrize("stdin,stdout,expected", [(True, True, True), (True, False, True),
                                                   (False, True, False), (False, False, False)])
def test_a_tty_follows_stdin_alone(stdin, stdout, expected, monkeypatch):
    """`osctl member add ... | tee` from a terminal still gets -t, so getpass in the container
    keeps the URL and whsec_ hidden instead of echoing them (MALU-221 review)."""
    monkeypatch.setattr(sys, "stdin", Tty(stdin))
    monkeypatch.setattr(sys, "stdout", Tty(stdout))
    assert dev.is_interactive() is expected


def test_exec_uses_a_tty_only_when_interactive():
    runner = FakeRunner("running")
    run(["exec"], runner, interactive=False)
    assert runner.calls[-1] == ["podman", "exec", "-i", "outer-steppes", "sh"]
    run(["exec", "ls", "-l", "/var/lib/outer-steppes"], runner)
    assert runner.calls[-1] == ["podman", "exec", "-it", "outer-steppes", "ls", "-l", "/var/lib/outer-steppes"]


def test_status_runs_osctl_status_when_running():
    runner = FakeRunner("running")
    code, out, _ = run(["status"], runner)
    assert code == 0 and "running" in out
    assert runner.calls[-1][-3:] == ["osctl", "bridge", "status"]
    runner = FakeRunner("absent")
    code, out, _ = run(["status"], runner)
    assert code == 1 and "not created" in out and not runner.commands("podman", "exec")


def test_tunnel_funnels_the_public_listener_in_the_foreground(env_file):
    runner = FakeRunner()
    code, out, err = run(["tunnel", "--env-file", str(env_file)], runner)
    assert code == 0 and "100.64.0.1" not in out + err and "other.tail0000" not in out + err
    # Never --bg (Funnel would outlive the session) and never the admin port.
    assert runner.commands("tailscale", "funnel") == [FUNNEL]


def test_tunnel_ctrl_c_is_a_clean_stop():
    class Interrupted(FakeRunner):
        def __call__(self, cmd, capture=False):
            if cmd[:2] == ["tailscale", "funnel"]:
                raise KeyboardInterrupt
            return super().__call__(cmd, capture)
    try:
        code, out, _ = run(["tunnel", "--url", BASE], Interrupted())
    except KeyboardInterrupt:  # escaping would abort pytest itself, not fail this test
        pytest.fail("Ctrl-C escaped tunnel as a traceback")
    assert code == 0 and "Funnel stopped" in out


def test_captured_output_is_read_as_utf8():
    # A peer named with a non-ASCII letter: "Á" is bytes C3 81, and 0x81 has no cp1252 mapping.
    script = "import sys; sys.stdout.buffer.write('{\"Peer\": \"Á\"}'.encode('utf-8'))"
    code, text = dev.default_runner([sys.executable, "-c", script], capture=True)
    assert code == 0 and json.loads(text) == {"Peer": "Á"}


def test_tunnel_url_flag_needs_no_env_file():
    runner = FakeRunner()
    code, _, _ = run(["tunnel", "--url", BASE.upper() + "/"], runner)
    assert code == 0 and runner.commands("tailscale", "funnel") == [FUNNEL]


@pytest.mark.parametrize("url", ["https://made-up-other.tail0000.ts.net", "https://made-up-name.tail9999.ts.net",
                                 "https://made-up-name.ngrok-free.app", "https://made-up-name.tail0000.ts.net.evil.example"])
def test_tunnel_refuses_a_url_that_isnt_this_machines_funnel_name(url):
    runner = FakeRunner()
    code, _, err = run(["tunnel", "--url", url], runner)
    assert code == 2 and "Funnel URL" in err and BASE in err and not runner.commands("tailscale", "funnel")


def test_tunnel_never_prints_a_mismatched_url_from_the_env_file(tmp_path):
    path = tmp_path / "other.env"
    path.write_text("PUBLIC_BASE_URL=https://made-up-private-name.example\n", encoding="utf-8")
    code, out, err = run(["tunnel", "--env-file", str(path)], FakeRunner())
    assert code == 2 and "made-up-private-name" not in out + err


@pytest.mark.parametrize("status, says", [
    (False, "tailscale status"),
    ("not json", "JSON"),
    ("[]", "tailscale up"),
    (ts_status(state="NeedsLogin"), "tailscale up"),
    (ts_status(name=""), "MagicDNS"),
    (ts_status(name="made-up-name.example."), "MagicDNS"),
    (json.dumps({"BackendState": "Running", "Self": "odd"}), "MagicDNS"),
])
def test_tunnel_needs_a_connected_tailscale_with_a_ts_net_name(status, says):
    runner = FakeRunner(tailscale=status)
    code, _, err = run(["tunnel", "--url", BASE], runner)
    assert code == 2 and says in err and not runner.commands("tailscale", "funnel")


@pytest.mark.parametrize("url", ["http://made-up.example", "https://made-up.example/path",
                                 "https://made-up.example:8443", "https://u@made-up.example", "made-up.example"])
def test_tunnel_refuses_anything_but_a_bare_https_origin(url):
    runner = FakeRunner()
    code, _, err = run(["tunnel", "--url", url], runner)
    assert code == 2 and "https origin" in err and not runner.commands("tailscale")


def test_tunnel_needs_a_url(tmp_path):
    path = tmp_path / "no-base.env"
    path.write_text(f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\n", encoding="utf-8")
    code, out, err = run(["tunnel", "--env-file", str(path)], FakeRunner())
    assert code == 2 and "PUBLIC_BASE_URL" in err and TOKEN not in out + err


def test_db_snapshot_copies_out_and_cleans_up(tmp_path):
    runner = FakeRunner("running")
    code, out, _ = run(["db-snapshot", "--out", str(tmp_path / "snaps")], runner)
    assert code == 0
    backup, cp, cleanup = runner.commands("podman", "exec")[0], runner.commands("podman", "cp")[0], \
        runner.commands("podman", "exec")[1]
    assert backup[:5] == ["podman", "exec", "outer-steppes", "python", "-c"]
    assert "DATABASE_PATH" in backup[5] and ".backup(" in backup[5] and "mode=rw" in backup[5]
    inner = backup[6]
    assert inner.startswith("/tmp/bridge-") and cp[2] == f"outer-steppes:{inner}"
    assert Path(cp[3]).parent == tmp_path / "snaps"
    assert cleanup == ["podman", "exec", "outer-steppes", "rm", "-f", inner]
    assert "whsec_" in out


def test_db_snapshot_cleans_up_when_the_copy_fails(tmp_path):
    runner = FakeRunner("running", fail=[("podman", "cp")])
    code, _, _ = run(["db-snapshot", "--out", str(tmp_path)], runner)
    assert code == 1
    assert runner.calls[-1][:5] == ["podman", "exec", "outer-steppes", "rm", "-f"]


def test_db_snapshot_refuses_the_repository():
    runner = FakeRunner("running")
    for out in (str(dev.REPO), str(dev.REPO / "data")):
        code, _, err = run(["db-snapshot", "--out", out], runner)
        assert code == 2 and "inside the repository" in err
    assert not runner.commands("podman", "exec") and not runner.commands("podman", "cp")


def test_db_snapshot_needs_a_running_container(tmp_path):
    code, _, err = run(["db-snapshot", "--out", str(tmp_path)], FakeRunner("exited"))
    assert code == 2 and "isn't running" in err


def test_restart_builds_before_stopping_and_keeps_running_if_the_build_fails(env_file):
    runner = FakeRunner("running")
    run(["restart", "--build", "--env-file", str(env_file)], runner)
    assert runner.calls.index(runner.commands("podman", "build")[0]) < runner.calls.index(
        ["podman", "stop", "outer-steppes"])
    runner = FakeRunner("running", fail=[("podman", "build")])
    code, _, _ = run(["restart", "--build", "--env-file", str(env_file)], runner)
    assert code == 1 and not runner.commands("podman", "stop")


def test_restart_names_a_found_dot_env_once(isolated_dirs):
    repo, _ = isolated_dirs
    (repo / ".env").write_text("X=1\n", encoding="utf-8")
    _, _, err = run(["restart"], FakeRunner("running"))
    assert err.count("env file:") == 1


def test_tunnel_reads_past_a_bom_and_takes_the_last_value(tmp_path):
    path = tmp_path / "bom.env"
    path.write_bytes(f"﻿PUBLIC_BASE_URL=https://first.example\nX=1\nPUBLIC_BASE_URL={BASE}\n".encode("utf-8"))
    runner = FakeRunner()
    code, _, _ = run(["tunnel", "--env-file", str(path)], runner)
    assert code == 0 and runner.commands("tailscale", "funnel") == [FUNNEL]


def test_tunnel_explains_a_utf16_env_file(tmp_path):
    path = tmp_path / "utf16.env"
    path.write_text(f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\nPUBLIC_BASE_URL={BASE}\n", encoding="utf-16")
    code, out, err = run(["tunnel", "--env-file", str(path)], FakeRunner())
    assert code == 2 and "UTF-8" in err and TOKEN not in out + err


def test_tunnel_refuses_a_malformed_port():
    runner = FakeRunner()
    code, _, err = run(["tunnel", "--url", "https://made-up.example:abc"], runner)
    assert code == 2 and "https origin" in err and not runner.commands("tailscale")


def test_db_snapshot_fails_cleanly_if_the_copy_is_missing(tmp_path):
    class NoFile(FakeRunner):
        def __call__(self, cmd, capture=False):
            if cmd[:2] == ["podman", "cp"]:
                self.calls.append(cmd)
                return 0, ""
            return super().__call__(cmd, capture)

    code, _, err = run(["db-snapshot", "--out", str(tmp_path)], NoFile("running"))
    assert code == 2 and "isn't there" in err


def test_up_build_says_a_running_container_keeps_the_old_image(env_file):
    runner = FakeRunner("running")
    code, out, _ = run(["up", "--build", "--env-file", str(env_file)], runner)
    assert code == 0 and "old image" in out and not runner.commands("podman", "run")
    assert not runner.commands("podman", "exec")


# ------------------------------------------------------------------ the claim link (DESIGN.md, Local development)

CLAIM = ["podman", "exec", "outer-steppes", "osctl", "bridge", "claim", "--if-unclaimed"]


@pytest.mark.parametrize("argv,state", [(["up"], "absent"), (["up"], "exited"), (["restart"], "running")])
def test_a_healthy_start_asks_the_admin_api_for_a_claim_link(env_file, argv, state):
    """Through the container's own osctl, not the logs; it stays quiet when there's nothing to claim."""
    runner = FakeRunner(state)
    code, _, err = run([*argv, "--env-file", str(env_file)], runner)
    assert code == 0 and not err
    assert runner.commands("podman", "exec") == [CLAIM]
    assert runner.calls.index(runner.commands("podman", "run")[0]) < runner.calls.index(CLAIM)
    # Not captured: the link must reach the terminal, and nothing here keeps a copy.
    assert runner.captured[runner.calls.index(CLAIM)] is False


def test_reset_asks_for_a_claim_link_on_the_empty_database(env_file, tmp_path):
    runner = FakeRunner("running")
    assert reset(runner, env_file, tmp_path, "--yes")[0] == 0
    assert runner.commands("podman", "exec") == [CLAIM]


def test_an_already_running_container_keeps_its_claim_link(env_file):
    """Minting would replace a link the owner may be signing in with, so it only points at the command."""
    runner = FakeRunner("running")
    code, out, _ = run(["up", "--env-file", str(env_file)], runner)
    assert code == 0 and "`osctl bridge claim`" in out
    assert not runner.commands("podman", "exec")


def test_an_unhealthy_start_asks_for_no_claim_link(env_file):
    runner = FakeRunner()
    assert run(["up", "--env-file", str(env_file)], runner, healthy=False)[0] == 1
    assert not runner.commands("podman", "exec")


def test_a_failed_claim_link_warns_but_the_container_is_up(env_file):
    runner = FakeRunner(fail=[tuple(CLAIM)])
    code, _, err = run(["up", "--env-file", str(env_file)], runner)
    assert code == 0 and "`osctl bridge claim` tries again" in err


def test_tunnel_refuses_port_zero():
    code, _, _ = run(["tunnel", "--url", "https://made-up.example:0"], FakeRunner())
    assert code == 2


def test_dockerignore_excludes_env_files_at_any_depth():
    lines = (REAL_REPO / ".dockerignore").read_text(encoding="utf-8").splitlines()
    assert "**/.env" in lines and "**/*.env" in lines


def test_no_other_ignore_file_can_replace_the_dockerignore():
    """Podman reads a .containerignore instead of .dockerignore, which would drop the .env exclusions.
    Docker-style per-file ignore files (Dockerfile.dockerignore) would also replace it, so none of those either."""
    found = []
    for root, dirs, files in os.walk(REAL_REPO):
        dirs[:] = [d for d in dirs if d not in (".git", ".venv")]
        found += [Path(root, f).relative_to(REAL_REPO).as_posix() for f in files
                  if f.endswith((".containerignore", ".dockerignore"))]
    assert found == [".dockerignore"]


def test_a_relative_snapshot_dir_is_resolved_from_the_cwd(isolated_dirs):
    _, cwd = isolated_dirs
    runner = FakeRunner("running")
    code, _, _ = run(["db-snapshot", "--out", "snaps"], runner)
    assert code == 0
    dest = Path(runner.commands("podman", "cp")[0][3])
    assert dest.is_absolute() and dest.parent == (cwd / "snaps").resolve()


def test_a_relative_snapshot_dir_inside_the_repo_is_refused(isolated_dirs, monkeypatch):
    repo, _ = isolated_dirs
    monkeypatch.chdir(repo)
    runner = FakeRunner("running")
    for out in (".", "data"):
        code, _, err = run(["db-snapshot", "--out", out], runner)
        assert code == 2 and "inside the repository" in err
    assert not runner.commands("podman", "cp")


def test_a_relative_env_file_is_passed_to_podman_as_absolute(isolated_dirs):
    _, cwd = isolated_dirs
    (cwd / "my.env").write_text("X=1\n", encoding="utf-8")
    runner = FakeRunner()
    run(["up", "--env-file", "my.env"], runner)
    assert env_run(runner) == str((cwd / "my.env").resolve())


def test_up_warns_about_keys_the_image_owns_by_name_only(tmp_path):
    path = tmp_path / "venv-style.env"
    path.write_text("PUBLIC_BIND=127.0.0.1:18123\nDATABASE_PATH=made-up-dir/bridge.db\n"
                    "OSCTL_ADMIN_URL=http://127.0.0.1:18124\n"
                    f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\n", encoding="utf-8")
    code, out, err = run(["up", "--env-file", str(path)], FakeRunner())
    assert "DATABASE_PATH, OSCTL_ADMIN_URL, PUBLIC_BIND" in err
    assert "18123" not in out + err and "18124" not in out + err
    assert "made-up-dir" not in out + err and TOKEN not in out + err


def test_an_unreadable_env_file_is_a_clean_error(tmp_path):
    with pytest.raises(dev.DevError, match="can't read"):
        dev.env_lines(tmp_path)  # a directory: IsADirectoryError or PermissionError


def test_up_is_quiet_about_a_clean_env_file(env_file):
    _, _, err = run(["up", "--env-file", str(env_file)], FakeRunner())
    assert "warning" not in err


def test_tunnel_takes_a_bare_key_from_the_host_environment(tmp_path):
    path = tmp_path / "bare.env"
    path.write_text("# PUBLIC_BASE_URL=https://commented.example\nPUBLIC_BASE_URL\n", encoding="utf-8")
    runner = FakeRunner()
    code, _, _ = run(["tunnel", "--env-file", str(path)], runner, env={"PUBLIC_BASE_URL": BASE})
    assert code == 0 and runner.commands("tailscale", "funnel") == [FUNNEL]


def test_restart_reads_the_env_file_before_stopping(tmp_path):
    path = tmp_path / "utf16.env"
    path.write_text("X=1\n", encoding="utf-16")
    runner = FakeRunner("running")
    code, _, err = run(["restart", "--env-file", str(path)], runner)
    assert code == 2 and "UTF-8" in err and not runner.commands("podman", "stop")


@pytest.mark.parametrize("key", ["DISCORD_BOT_TOKEN", "LOG_PROMPTS"])
def test_up_and_restart_refuse_a_key_the_bridge_refuses_before_touching_podman(tmp_path, key):
    path = tmp_path / "bad.env"
    path.write_text(f"{key}=made-up-forbidden-value\nX=1\n", encoding="utf-8")
    for argv, state in ((["up", "--build"], "absent"), (["restart", "--build"], "running")):
        runner = FakeRunner(state)
        code, out, err = run([*argv, "--env-file", str(path)], runner)
        assert code == 2 and key in err and "made-up-forbidden-value" not in out + err
        assert not [c for c in runner.calls if c[:2] in (["podman", "run"], ["podman", "stop"], ["podman", "build"])]


def test_up_and_restart_refuse_a_bom_before_touching_podman(tmp_path):
    """podman --env-file keeps the BOM in the first key, so the bridge would silently lose that setting."""
    path = tmp_path / "bom.env"
    path.write_text(f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\n", encoding="utf-8-sig")
    for argv, state in ((["up", "--build"], "absent"), (["restart", "--build"], "running")):
        runner = FakeRunner(state)
        code, out, err = run([*argv, "--env-file", str(path)], runner)
        assert code == 2 and "BOM" in err and TOKEN not in out + err
        assert not [c for c in runner.calls if c[:2] in (["podman", "run"], ["podman", "stop"], ["podman", "build"])]
    path.write_text(f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\n", encoding="utf-8")
    assert run(["up", "--env-file", str(path)], FakeRunner())[0] == 0


@pytest.mark.parametrize("line", ["DISCORD_*", "*", "MADE_UP_*KEY", "MADE_UP_KEY*=made-up-value"])
def test_up_and_restart_refuse_a_wildcard_key(tmp_path, line):
    """Podman fills "DISCORD_*" (or "*") from the host environment, so a bot token in the shell would reach the
    container's config without ever being named in the file."""
    path = tmp_path / "wild.env"
    path.write_text(f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\n\n{line}\n", encoding="utf-8")
    for argv, state in ((["up", "--build"], "absent"), (["restart", "--build"], "running")):
        runner = FakeRunner(state)
        code, out, err = run([*argv, "--env-file", str(path)], runner)
        assert code == 2 and '"*"' in err and "on line 3," in err and TOKEN not in out + err
        assert "made-up-value" not in out + err
        assert not [c for c in runner.calls if c[:2] in (["podman", "run"], ["podman", "stop"], ["podman", "build"])]


@pytest.mark.parametrize("line", ["discord_bot_token", "Discord_Bot_Token=made-up-value", "log_prompts=1"])
def test_up_and_restart_refuse_a_refused_key_in_any_case(tmp_path, line):
    """Windows looks up environment names in any case, so a bare "discord_bot_token" is filled from the shell's
    DISCORD_BOT_TOKEN and saved in the container's config."""
    path = tmp_path / "case.env"
    path.write_text(f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\n{line}\n", encoding="utf-8")
    for argv, state in ((["up", "--build"], "absent"), (["restart", "--build"], "running")):
        runner = FakeRunner(state)
        code, out, err = run([*argv, "--env-file", str(path)], runner)
        assert code == 2 and line.split("=")[0].upper() in err and "made-up-value" not in out + err
        assert not [c for c in runner.calls if c[:2] in (["podman", "run"], ["podman", "stop"], ["podman", "build"])]


def test_up_and_restart_refuse_an_empty_key(tmp_path):
    """podman run would refuse it only after restart had stopped the bridge, and print the value."""
    path = tmp_path / "empty.env"
    path.write_text(f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\n = made-up-value\n", encoding="utf-8")
    for argv, state in ((["up", "--build"], "absent"), (["restart", "--build"], "running")):
        runner = FakeRunner(state)
        code, out, err = run([*argv, "--env-file", str(path)], runner)
        assert code == 2 and "on line 2" in err and "made-up-value" not in out + err
        assert not [c for c in runner.calls if c[:2] in (["podman", "run"], ["podman", "stop"], ["podman", "build"])]


def test_inspect_asks_for_a_container_not_the_image_of_the_same_name():
    runner = FakeRunner("absent")
    run(["status"], runner)
    run(["down"], runner)
    inspects = runner.commands("podman", "inspect")
    assert len(inspects) == 2 and all(c[2:4] == ["--type", "container"] for c in inspects)


@pytest.mark.parametrize("line", ["MADE_UP_KEY =https://made-up.example", "MADE_UP_KEY\t= made-up-value",
                                  "made-up-secret-fragment =x"])
def test_up_and_restart_refuse_a_space_before_the_equals_sign(tmp_path, line):
    """podman --env-file keeps the space in the key, so the bridge would silently lose that setting.
    Only the line number is named: on a malformed line, the text before "=" could be part of a value."""
    path = tmp_path / "spaced.env"
    path.write_text(f"BRIDGE_REGISTRATION_TOKEN={TOKEN}\n# comment = x\n{line}\n", encoding="utf-8")
    for argv, state in ((["up", "--build"], "absent"), (["restart", "--build"], "running")):
        runner = FakeRunner(state)
        code, out, err = run([*argv, "--env-file", str(path)], runner)
        assert code == 2 and "on line 3," in err and TOKEN not in out + err
        assert "MADE_UP_KEY" not in out + err and "made-up-" not in out + err.replace(str(path), "")
        assert not [c for c in runner.calls if c[:2] in (["podman", "run"], ["podman", "stop"], ["podman", "build"])]
    path.write_text(f"  BRIDGE_REGISTRATION_TOKEN={TOKEN}\nX= 1\n", encoding="utf-8")  # Podman trims these
    assert run(["up", "--env-file", str(path)], FakeRunner())[0] == 0


def test_refused_keys_match_the_server():
    import outer_steppes.config as config
    assert dev.REFUSED_KEYS == set(config.FORBIDDEN_VARS)


def test_image_owned_keys_match_the_dockerfile():
    text = (REAL_REPO / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    block = text[text.index("ENV PUBLIC_BIND"):].split("\n\n")[0]
    keys = {part.split("=")[0].strip() for part in block.replace("ENV", "").replace("\\", "").split()}
    assert dev.IMAGE_OWNED_KEYS == keys


def test_an_in_repo_env_file_with_another_name_gets_a_warning(isolated_dirs):
    repo, _ = isolated_dirs
    (repo / "bridge.conf").write_text("X=1\n", encoding="utf-8")
    (repo / "local.env").write_text("X=1\n", encoding="utf-8")
    _, _, err = run(["up", "--env-file", str(repo / "bridge.conf")], FakeRunner())
    assert "isn't named .env" in err
    _, _, err = run(["up", "--env-file", str(repo / "local.env")], FakeRunner())
    assert "isn't named" not in err


# -- reset ----------------------------------------------------------------------

DELETES = (("podman", "stop"), ("podman", "rm"), ("podman", "volume", "rm"), ("podman", "run"))


def touched(runner):
    """Every command that stops, deletes or starts something."""
    return [c for p in DELETES for c in runner.commands(*p)]


def reset(runner, env_file, tmp_path, *flags, **kw):
    kw.setdefault("env", {"OS_SNAPSHOT_DIR": str(tmp_path / "snaps")})
    return run(["reset", "--env-file", str(env_file), *flags], runner, **kw)


def test_reset_confirms_then_exports_before_deleting_then_starts(env_file, tmp_path):
    runner = FakeRunner("running")
    code, out, _ = reset(runner, env_file, tmp_path, answer="outer-steppes-data")
    assert code == 0
    steps = [c[:3] for c in runner.calls if c[:2] == ["podman", "stop"] or c[:2] == ["podman", "volume"]
             or c[:2] == ["podman", "run"] and c != RUN_HELP]
    assert steps == [["podman", "stop", "outer-steppes"], ["podman", "volume", "exists"],
                     ["podman", "volume", "export"], ["podman", "volume", "rm"], ["podman", "run", "-d"]]
    [export] = runner.commands("podman", "volume", "export")
    dest = Path(export[export.index("--output") + 1])
    assert export[3] == "outer-steppes-data" and dest.parent == tmp_path / "snaps" and dest.is_file()
    assert runner.commands("podman", "volume", "rm") == [["podman", "volume", "rm", "outer-steppes-data"]]
    assert str(dest) in out and "whsec_" in out


@pytest.mark.parametrize("answer", ["", "yes", "y", "outer-steppes", "OUTER-STEPPES-DATA"])
def test_reset_needs_the_volume_name_typed(env_file, tmp_path, answer):
    runner = FakeRunner("running")
    code, out, _ = reset(runner, env_file, tmp_path, answer=answer)
    assert code == 1 and "nothing changed" in out
    assert touched(runner) == [] and not runner.commands("podman", "volume")


def test_reset_without_a_terminal_needs_yes(env_file, tmp_path):
    runner = FakeRunner("running")
    code, _, err = reset(runner, env_file, tmp_path, interactive=False)
    assert code == 2 and "--yes" in err and touched(runner) == []
    code, _, _ = reset(runner, env_file, tmp_path, "--yes", interactive=False)
    assert code == 0 and runner.commands("podman", "volume", "export") and runner.commands("podman", "volume", "rm")


def test_a_failed_export_deletes_nothing(env_file, tmp_path):
    runner = FakeRunner("running", fail=[("podman", "volume", "export")])
    code, _, err = reset(runner, env_file, tmp_path, answer="outer-steppes-data")
    assert code != 0 and "nothing was deleted" in err
    assert not runner.commands("podman", "volume", "rm") and not runner.commands("podman", "run")


def test_reset_no_snapshot_deletes_without_exporting(env_file, tmp_path):
    runner = FakeRunner("running")
    code, out, _ = reset(runner, env_file, tmp_path, "--no-snapshot", answer="outer-steppes-data")
    assert code == 0 and "No copy is kept" in out
    assert not runner.commands("podman", "volume", "export") and runner.commands("podman", "volume", "rm")


def test_reset_checks_the_env_file_before_asking_or_touching_anything(tmp_path):
    bad = tmp_path / "bad.env"
    bad.write_text("DISCORD_BOT_TOKEN=made-up\n", encoding="utf-8")
    runner = FakeRunner("running")
    code, _, err = reset(runner, bad, tmp_path)  # answer=None: asking fails the test
    assert code == 2 and "DISCORD_BOT_TOKEN" in err and touched(runner) == []


def test_reset_builds_before_stopping_and_stops_if_the_build_fails(env_file, tmp_path):
    runner = FakeRunner("running", fail=[("podman", "build")])
    code, _, _ = reset(runner, env_file, tmp_path, "--build", answer="outer-steppes-data")
    assert code == 1 and runner.commands("podman", "build") and touched(runner) == []


def test_reset_refuses_a_snapshot_dir_inside_the_repository_before_asking(isolated_dirs, env_file, tmp_path):
    repo, _ = isolated_dirs
    runner = FakeRunner("running")
    code, _, err = reset(runner, env_file, tmp_path, env={"OS_SNAPSHOT_DIR": str(repo / "snaps")})
    assert code == 2 and "inside the repository" in err and touched(runner) == []


def test_reset_uses_os_volume(env_file, tmp_path):
    runner = FakeRunner("running")
    env = {"OS_SNAPSHOT_DIR": str(tmp_path / "snaps"), "OS_VOLUME": "os-dev-data"}
    assert reset(runner, env_file, tmp_path, env=env, answer="outer-steppes-data")[0] == 1  # the default name
    assert touched(runner) == []
    assert reset(runner, env_file, tmp_path, env=env, answer="os-dev-data")[0] == 0
    assert runner.commands("podman", "volume", "rm") == [["podman", "volume", "rm", "os-dev-data"]]
    [cmd] = runner.commands("podman", "run")
    assert cmd[cmd.index("-v") + 1] == "os-dev-data:/var/lib/outer-steppes"


def test_reset_with_no_volume_just_starts(env_file, tmp_path):
    runner = FakeRunner("absent", volume=False)
    code, _, _ = reset(runner, env_file, tmp_path, answer="outer-steppes-data")
    assert code == 0 and runner.commands("podman", "run")
    assert not runner.commands("podman", "volume", "export") and not runner.commands("podman", "volume", "rm")


def test_reset_treats_ctrl_d_as_not_confirmed(env_file, tmp_path):
    runner = FakeRunner("running")
    code, out, _ = reset(runner, env_file, tmp_path, answer=EOFError)
    assert code == 1 and "nothing changed" in out and touched(runner) == []


class PartialExport(FakeRunner):
    """`podman volume export` writes part of the tar, then fails."""

    def __call__(self, cmd, capture=False):
        if cmd[:3] == ["podman", "volume", "export"]:
            self.calls.append(cmd)
            Path(cmd[cmd.index("--output") + 1]).write_bytes(b"made-up partial")
            return 1, ""
        return super().__call__(cmd, capture)


def test_a_failed_export_removes_its_partial_copy(env_file, tmp_path):
    runner = PartialExport("running")
    code, _, err = reset(runner, env_file, tmp_path, answer="outer-steppes-data")
    assert code == 1 and "nothing was deleted" in err
    assert list((tmp_path / "snaps").iterdir()) == [] and not runner.commands("podman", "volume", "rm")


def test_a_podman_error_checking_the_volume_deletes_nothing(env_file, tmp_path):
    runner = FakeRunner("running", volume=None)
    code, _, err = reset(runner, env_file, tmp_path, answer="outer-steppes-data")
    assert code == 2 and "couldn't check" in err
    assert not runner.commands("podman", "volume", "export") and not runner.commands("podman", "volume", "rm")
    assert not runner.commands("podman", "run")


def test_a_failed_volume_removal_says_so_and_starts_nothing(env_file, tmp_path):
    runner = FakeRunner("running", fail=[("podman", "volume", "rm")])
    code, _, err = reset(runner, env_file, tmp_path, answer="outer-steppes-data")
    assert code == 1 and "removing outer-steppes-data failed" in err and not runner.commands("podman", "run")


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes; Windows relies on the profile's ACL")
def test_reset_and_db_snapshot_write_their_copies_0600(env_file, tmp_path):
    """Both copies hold every whsec_ and automation URL (DESIGN.md, Local development)."""
    import stat
    old_umask = os.umask(0o022)  # files would otherwise be created 0644
    try:
        runner = FakeRunner("running")
        assert run(["db-snapshot", "--out", str(tmp_path / "db")], runner)[0] == 0
        assert reset(runner, env_file, tmp_path, answer="outer-steppes-data")[0] == 0
    finally:
        os.umask(old_umask)
    copies = list((tmp_path / "db").iterdir()) + list((tmp_path / "snaps").iterdir())
    assert len(copies) == 2
    assert all(stat.S_IMODE(c.stat().st_mode) == 0o600 for c in copies), copies


def test_reset_yes_skips_the_prompt_on_a_terminal_and_still_says_what_it_deletes(env_file, tmp_path):
    runner = FakeRunner("running")
    code, out, _ = reset(runner, env_file, tmp_path, "--yes", "--no-snapshot")  # answer=None: asking fails
    assert code == 0 and "This deletes the volume outer-steppes-data" in out
    assert runner.commands("podman", "volume", "rm")


def test_reset_points_at_restore_for_its_copy(env_file, tmp_path):
    _, out, _ = reset(FakeRunner("running"), env_file, tmp_path, "--yes")
    assert "`osctl container restore <file>`" in out and "podman volume import" not in out


# -- the empty-snapshot warning ------------------------------------------------

NO_MEMBERS = "the snapshot holds no members"


def test_reset_and_db_snapshot_warn_about_a_snapshot_with_no_members(env_file, tmp_path):
    """Found when a reset's copy turned out empty (MALU-257). A warning, never a prompt."""
    code, _, err = run(["db-snapshot", "--out", str(tmp_path / "db")], FakeRunner("running", members=0))
    assert code == 0 and NO_MEMBERS in err
    code, _, err = reset(FakeRunner("running", members=0), env_file, tmp_path, "--yes")  # answer=None: no prompt
    assert code == 0 and NO_MEMBERS in err


def test_a_snapshot_with_members_gets_no_warning(env_file, tmp_path):
    _, _, err = run(["db-snapshot", "--out", str(tmp_path / "db")], FakeRunner("running", members=2))
    assert "warning" not in err
    _, _, err = reset(FakeRunner("running", members=1), env_file, tmp_path, "--yes")
    assert "warning" not in err


@pytest.mark.parametrize("arcname", ["./bridge.db", "bridge.db", "/bridge.db"])
def test_member_count_reads_a_volume_export(tmp_path, arcname):
    (tmp_path / "snaps").mkdir()
    snap = made_up_tar(tmp_path / "snaps" / "v.tar", members=3, arcname=arcname)
    assert dev.member_count(snap) == 3
    assert [p.name for p in snap.parent.iterdir()] == ["v.tar"]  # nothing written beside it


def test_member_count_leaves_a_db_copy_alone(tmp_path):
    (tmp_path / "snaps").mkdir()
    snap = made_up_db(tmp_path / "snaps" / "bridge-x.db", members=0)
    before = snap.read_bytes()
    assert dev.member_count(snap) == 0
    assert snap.read_bytes() == before and [p.name for p in snap.parent.iterdir()] == ["bridge-x.db"]


@pytest.mark.parametrize("kind", ["tar", "db"])
def test_member_count_never_opens_the_snapshot_itself(tmp_path, monkeypatch, kind):
    """A db-snapshot copy is in WAL mode, so opening it in place puts a -wal and -shm beside it,
    which stay if the process dies. Only a private temporary copy is opened."""
    (tmp_path / "snaps").mkdir()
    snap = (made_up_tar if kind == "tar" else made_up_db)(tmp_path / "snaps" / f"s.{kind}")
    opened = []
    real_connect = sqlite3.connect
    monkeypatch.setattr(dev.sqlite3, "connect", lambda target, *a, **kw: opened.append(target) or
                        real_connect(target, *a, **kw))
    assert dev.member_count(snap) == 1
    assert len(opened) == 1 and str(tmp_path / "snaps") not in opened[0].replace("/", os.sep)


def test_member_count_ignores_paths_outside_the_export(tmp_path):
    """Only the database is read, under a fixed name: an entry like ../bridge.db is never written out."""
    inner = made_up_db(tmp_path / "inner.db")
    with tarfile.open(tmp_path / "v.tar", "w") as tar:
        tar.add(inner, arcname="../bridge.db")
    with pytest.raises(dev.DevError, match="no bridge.db"):
        dev.member_count(tmp_path / "v.tar")


def test_an_unreadable_snapshot_warns_that_it_couldnt_check(tmp_path):
    class Garbage(FakeRunner):
        def __call__(self, cmd, capture=False):
            result = super().__call__(cmd, capture)
            if cmd[:2] == ["podman", "cp"]:
                Path(cmd[3]).write_bytes(b"made-up, not a database")
            return result

    code, _, err = run(["db-snapshot", "--out", str(tmp_path)], Garbage("running"))
    assert code == 0 and "couldn't check whether the snapshot holds any members" in err


# -- restore --------------------------------------------------------------------

SCRATCH = "outer-steppes-data-restore-check"
# Read-only, and run before the confirmation: without the image nothing can check the snapshot.
IMAGE_EXISTS = ["podman", "image", "exists", "outer-steppes"]


class RestoreRunner(FakeRunner):
    """Also answers the schema check, run with `--entrypoint python`, with the given code and text."""

    def __init__(self, *args, check=(0, ""), **kw):
        super().__init__(*args, **kw)
        self.check = check

    def __call__(self, cmd, capture=False):
        if cmd[:3] == ["podman", "run", "--rm"] and "python" in cmd and dev.SCHEMA_CHECK_SCRIPT in cmd:
            self.calls.append(cmd)
            self.captured.append(capture)
            return self.check
        return super().__call__(cmd, capture)


def restore(runner, env_file, tmp_path, snap, *flags, **kw):
    kw.setdefault("env", {"OS_SNAPSHOT_DIR": str(tmp_path / "snaps")})
    return run(["restore", str(snap), "--env-file", str(env_file), *flags], runner, **kw)


def volume_steps(runner):
    """The volume and container lifecycle, in order: what's created, loaded, fixed, checked, stopped or removed."""
    steps = []
    for c in runner.calls:
        if c == RUN_HELP or c[:3] in (["podman", "volume", "exists"], ["podman", "volume", "inspect"]):
            continue
        if c[:2] == ["podman", "volume"]:
            c = [a for i, a in enumerate(c) if a != "--label" and c[i - 1] != "--label"]
            steps.append(" ".join(c[2:4]))
        elif c[:3] == ["podman", "run", "--rm"]:
            vol = c[c.index("-v") + 1].split(":")[0]
            steps.append(("fix " if "--user" in c else "check ") + vol)
        elif c[:3] == ["podman", "run", "-d"]:
            steps.append("start")
        elif c[:2] == ["podman", "stop"]:
            steps.append("stop")
    return steps


@pytest.mark.parametrize("kind", ["tar", "db"])
def test_restore_checks_in_a_scratch_volume_before_stopping_then_replaces_and_starts(env_file, tmp_path, kind):
    snap = made_up_tar(tmp_path / "old.tar") if kind == "tar" else made_up_db(tmp_path / "old.db")
    runner = RestoreRunner("running")
    code, out, _ = restore(runner, env_file, tmp_path, snap, answer="outer-steppes-data")
    assert code == 0
    assert volume_steps(runner) == [
        f"create {SCRATCH}", f"import {SCRATCH}", f"fix {SCRATCH}", f"check {SCRATCH}", f"rm {SCRATCH}",
        "stop", "export outer-steppes-data", "rm outer-steppes-data",
        "create outer-steppes-data", "import outer-steppes-data", "fix outer-steppes-data", "start"]
    # The bridge reads the database as the image's user, from a 0700 directory (db.check_file_modes).
    [fix, _] = [c for c in runner.commands("podman", "run", "--rm") if "--user" in c]
    assert fix[fix.index("--user") + 1] == "0" and fix[-2:] == ["-c", dev.FIX_OWNER_SCRIPT]
    assert "chown -R outer-steppes:outer-steppes /var/lib/outer-steppes" in dev.FIX_OWNER_SCRIPT
    assert "chmod -R go-rwx /var/lib/outer-steppes" in dev.FIX_OWNER_SCRIPT
    [check] = [c for c in runner.commands("podman", "run", "--rm") if dev.SCHEMA_CHECK_SCRIPT in c]
    assert "--user" not in check and check[-1] == "/var/lib/outer-steppes/bridge.db"
    # Both loads read the same private copy, gone afterwards: the volume gets exactly what was checked.
    # A .db goes in as bridge.db.
    scratch_import, real_import = runner.commands("podman", "volume", "import")
    tar = Path(real_import[-1])
    assert scratch_import[-1] == real_import[-1] != str(snap)
    assert tar.name == "volume.tar" and not tar.exists()
    assert "This replaces the volume outer-steppes-data" in out and runner.commands("podman", "exec") == [CLAIM]
    assert "members removed, secrets rotated, connector connections revoked" in out


def test_every_throwaway_container_is_offline_unpulled_and_keeps_the_shell_out(env_file, tmp_path):
    runner = RestoreRunner("running")
    restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")
    throwaways = runner.commands("podman", "run", "--rm")
    assert len(throwaways) == 3
    for cmd in throwaways:
        assert cmd[cmd.index("--pull") + 1] == "never" and cmd[cmd.index("--network") + 1] == "none"
        assert "--env-host=false" in cmd and "--env-file" not in cmd and "-p" not in cmd


def test_restore_names_the_extra_tables_and_changes_nothing(env_file, tmp_path):
    runner = RestoreRunner("running", check=(3, "extra tables: grant_acks, grants\n"))
    code, _, err = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")
    assert code == 2 and "extra tables: grant_acks, grants" in err and "doesn't migrate" in err
    assert volume_steps(runner)[-1] == f"rm {SCRATCH}"  # the scratch volume goes either way
    assert not runner.commands("podman", "stop") and not runner.commands("podman", "volume", "export")
    assert not [c for c in runner.commands("podman", "volume") if "outer-steppes-data" in c]


def test_restore_without_the_image_says_to_build_before_asking(env_file, tmp_path):
    runner = RestoreRunner("running", fail=[("podman", "image", "exists")])
    code, _, err = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"))  # answer=None
    assert code == 2 and "osctl container build" in err
    assert runner.calls == [IMAGE_EXISTS]


def test_a_failed_schema_check_changes_nothing(env_file, tmp_path):
    runner = RestoreRunner("running", check=(125, ""))
    code, _, err = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")
    assert code == 2 and "checking old.tar failed, so nothing changed" in err
    assert not runner.commands("podman", "stop") and volume_steps(runner)[-1] == f"rm {SCRATCH}"


def test_restore_stops_if_the_scratch_volume_cant_be_loaded(env_file, tmp_path):
    runner = RestoreRunner("running", fail=[("podman", "volume", "import", SCRATCH)])
    code, _, err = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")
    assert code == 2 and "nothing changed" in err
    assert not runner.commands("podman", "stop") and volume_steps(runner)[-1] == f"rm {SCRATCH}"


def test_a_failed_label_lookup_never_hides_the_real_error(env_file, tmp_path):
    """The scratch volume restore just made is removed without asking for its label again."""
    class NoInspect(RestoreRunner):
        def __call__(self, cmd, capture=False):
            if cmd[:3] == ["podman", "volume", "inspect"]:
                self.calls.append(cmd)
                return 125, ""
            return super().__call__(cmd, capture)

    runner = NoInspect("running", check=(3, "extra tables: grants\n"))
    code, _, err = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")
    assert code == 2 and "extra tables: grants" in err and "didn't make it" not in err
    assert volume_steps(runner)[-1] == f"rm {SCRATCH}"


def test_a_scratch_volume_that_wont_go_is_named(env_file, tmp_path):
    """It holds every whsec_ the snapshot does."""
    runner = RestoreRunner("running", fail=[("podman", "volume", "rm", SCRATCH)])
    _, _, err = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")
    assert f"couldn't remove the scratch volume {SCRATCH}" in err and f"podman volume rm {SCRATCH}" in err


def test_restore_never_removes_a_volume_it_didnt_make(env_file, tmp_path):
    """Only a volume with restore's label is a leftover; another of the same name is someone else's."""
    runner = RestoreRunner("running")
    runner.scratch_exists = True
    code, _, err = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")
    assert code == 2 and f"{SCRATCH} exists but restore didn't make it" in err
    assert not runner.commands("podman", "volume", "rm") and not runner.commands("podman", "stop")


def test_the_scratch_volume_is_labeled_and_the_real_one_isnt(env_file, tmp_path):
    runner = RestoreRunner("running")
    restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")
    assert runner.commands("podman", "volume", "create") == [
        ["podman", "volume", "create", "--label", "outer-steppes.restore-check=1", SCRATCH],
        ["podman", "volume", "create", "outer-steppes-data"]]


def test_restore_removes_a_scratch_volume_left_over_from_before(env_file, tmp_path):
    runner = RestoreRunner("running")
    runner.scratch_exists = runner.scratch_labeled = True
    assert restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")[0] == 0
    assert volume_steps(runner)[:2] == [f"rm {SCRATCH}", f"create {SCRATCH}"]


@pytest.mark.parametrize("answer", ["", "yes", "outer-steppes", EOFError])
def test_restore_needs_the_volume_name_typed(env_file, tmp_path, answer):
    runner = RestoreRunner("running")
    code, out, _ = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), answer=answer)
    assert code == 1 and "nothing changed" in out and runner.calls == [IMAGE_EXISTS]


def test_restore_without_a_terminal_needs_yes(env_file, tmp_path):
    runner = RestoreRunner("running")
    code, _, err = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), interactive=False)
    assert code == 2 and "restore deletes the volume" in err and "--yes" in err and runner.calls == [IMAGE_EXISTS]


@pytest.mark.parametrize("name", ["old.sqlite", "old.tar.gz", "missing.tar"])
def test_restore_refuses_a_file_that_isnt_a_snapshot_before_asking(env_file, tmp_path, name):
    if name != "missing.tar":
        (tmp_path / name).write_bytes(b"made-up")
    runner = RestoreRunner("running")
    code, _, err = restore(runner, env_file, tmp_path, tmp_path / name)  # answer=None: asking fails
    assert code == 2 and runner.calls == []
    assert ("not found" if name == "missing.tar" else ".tar from") in err


def test_restore_checks_the_env_file_and_snapshot_dir_before_asking(isolated_dirs, tmp_path):
    repo, _ = isolated_dirs
    snap = made_up_tar(tmp_path / "old.tar")
    bad = tmp_path / "bad.env"
    bad.write_text("DISCORD_BOT_TOKEN=made-up\n", encoding="utf-8")
    runner = RestoreRunner("running")
    assert restore(runner, bad, tmp_path, snap)[0] == 2
    good = tmp_path / "good.env"
    good.write_text("X=1\n", encoding="utf-8")
    code, _, err = restore(runner, good, tmp_path, snap, env={"OS_SNAPSHOT_DIR": str(repo / "snaps")})
    assert code == 2 and "inside the repository" in err and runner.calls == [IMAGE_EXISTS]


def test_restore_no_snapshot_replaces_without_exporting(env_file, tmp_path):
    runner = RestoreRunner("running")
    code, out, _ = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--no-snapshot", "--yes")
    assert code == 0 and "No copy is kept" in out and not runner.commands("podman", "volume", "export")
    assert "rm outer-steppes-data" in volume_steps(runner)


def test_restore_onto_no_volume_just_loads_it(env_file, tmp_path):
    runner = RestoreRunner("absent", volume=False)
    assert restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")[0] == 0
    assert volume_steps(runner)[-4:] == ["create outer-steppes-data", "import outer-steppes-data",
                                         "fix outer-steppes-data", "start"]


def test_a_failed_export_of_the_current_volume_replaces_nothing(env_file, tmp_path):
    runner = RestoreRunner("running", fail=[("podman", "volume", "export")])
    code, _, err = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")
    assert code == 1 and "nothing was deleted" in err
    assert ["podman", "volume", "rm", "outer-steppes-data"] not in runner.calls
    assert not runner.commands("podman", "run", "-d")


def test_a_failed_load_after_the_old_volume_is_gone_says_to_run_restore_again(env_file, tmp_path):
    runner = RestoreRunner("running", fail=[("podman", "volume", "import", "outer-steppes-data")])
    code, _, err = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")
    assert code == 1 and "osctl container restore` again" in err and not runner.commands("podman", "run", "-d")
    assert "exported above holds the replaced data" in err


def test_restore_uses_os_volume(env_file, tmp_path):
    runner = RestoreRunner("running")
    env = {"OS_SNAPSHOT_DIR": str(tmp_path / "snaps"), "OS_VOLUME": "os-dev-data"}
    assert restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes", env=env)[0] == 0
    assert "create os-dev-data-restore-check" in volume_steps(runner)
    [start] = runner.commands("podman", "run", "-d")
    assert start[start.index("-v") + 1] == "os-dev-data:/var/lib/outer-steppes"


def test_restore_exports_the_current_volume_with_the_empty_warning(env_file, tmp_path):
    """Restoring onto a just-reset volume: its copy is empty, and the warning says so."""
    runner = RestoreRunner("running", members=0)
    code, _, err = restore(runner, env_file, tmp_path, made_up_tar(tmp_path / "old.tar"), "--yes")
    assert code == 0 and NO_MEMBERS in err


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes; Windows relies on the profile's ACL")
def test_the_tar_made_from_a_db_is_private(env_file, tmp_path):
    import stat
    modes = []

    class Watch(RestoreRunner):
        def __call__(self, cmd, capture=False):
            if cmd[:3] == ["podman", "volume", "import"]:
                tar = Path(cmd[-1])
                modes.append((stat.S_IMODE(tar.stat().st_mode), stat.S_IMODE(tar.parent.stat().st_mode)))
                with tarfile.open(tar) as t:
                    modes.append(t.getmember("bridge.db").mode)
            return super().__call__(cmd, capture)

    old_umask = os.umask(0o022)
    try:
        restore(Watch("running"), env_file, tmp_path, made_up_db(tmp_path / "old.db"), "--yes")
    finally:
        os.umask(old_umask)
    assert modes[0] == (0o600, 0o700) and modes[1] == 0o600


# -- the schema check itself, run here against real databases --------------------


def schema_check(path):
    done = subprocess.run([sys.executable, "-c", dev.SCHEMA_CHECK_SCRIPT, str(path)],
                          capture_output=True, text=True)
    return done.returncode, done.stdout.strip()


def test_the_schema_check_passes_a_database_of_this_schema(tmp_path):
    assert schema_check(made_up_db(tmp_path / "bridge.db")) == (0, "")


def test_the_schema_check_names_extra_and_missing_tables(tmp_path):
    """A snapshot from before MALU-224 still has grants and grant_acks."""
    path = made_up_db(tmp_path / "bridge.db", extra_table="grants")
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE grant_acks (x)")
        conn.execute("DROP TABLE web_sessions")
        conn.commit()
    assert schema_check(path) == (3, "extra tables: grant_acks, grants; missing tables: web_sessions")


def test_the_schema_check_names_a_wrong_version(tmp_path):
    path = made_up_db(tmp_path / "bridge.db")
    with contextlib.closing(sqlite3.connect(path)) as conn:
        conn.execute("PRAGMA user_version = 7")
    assert schema_check(path) == (3, "schema version 7, expected 1")


def test_the_schema_check_refuses_a_missing_or_unreadable_file(tmp_path):
    assert schema_check(tmp_path / "bridge.db") == (3, "it has no bridge.db")
    assert not (tmp_path / "bridge.db").exists()  # mode=rw: never creates one
    (tmp_path / "bridge.db").write_bytes(b"made-up, not a database" * 100)
    code, text = schema_check(tmp_path / "bridge.db")
    assert code == 3 and text.startswith("it is not a readable SQLite database")


def test_database_path_and_user_match_the_dockerfile():
    text = (REAL_REPO / "deploy" / "Dockerfile").read_text(encoding="utf-8")
    assert f"DATABASE_PATH={dev.DATA_DIR}/{dev.DB_NAME}" in text
    assert f"USER {dev.IMAGE_USER}\n" in text and f"--uid 10001 --gid {dev.IMAGE_USER} " in text
