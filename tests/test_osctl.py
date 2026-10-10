"""osctl: secrets never come from argv; commands map to the admin API; groups route where DESIGN.md (Registration (operator)) says."""

from __future__ import annotations

import importlib.machinery
import importlib.util
import io
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "osctl.py"
spec = importlib.util.spec_from_file_location("osctl", SCRIPT)
osctl = importlib.util.module_from_spec(spec)
spec.loader.exec_module(osctl)

URL = "https://hooks.example.test/trigger/abc"
SECRET = "whsec_MDEyMzQ1Njc4OWFiY2RlZg=="


class FakeClient:
    def __init__(self, responses=None):
        self.calls = []
        self.responses = responses or {}

    def request(self, method, path, body=None):
        self.calls.append((method, path, body))
        response = self.responses.get((method, path), (200, {}))
        return response.pop(0) if isinstance(response, list) else response


def _all_parsers(parser):
    yield parser
    for action in parser._actions:
        if isinstance(action, osctl.argparse._SubParsersAction):
            for child in action.choices.values():
                yield from _all_parsers(child)


def test_parser_has_no_option_that_takes_a_url_or_secret():
    parser = osctl.build_parser()
    options = [opt for p in _all_parsers(parser) for a in p._actions for opt in a.option_strings]
    assert "--from-file" in options and "--env-file" in options  # the walk reached the leaves
    assert not any("secret" in o or "url" in o or "token" in o for o in options), options
    with pytest.raises(SystemExit):
        parser.parse_args(["member", "add", "123", "--signing-secret", SECRET])


def test_register_reads_url_and_secret_from_hidden_prompt():
    prompts = []

    def fake_getpass(prompt):
        prompts.append(prompt)
        return URL if "URL" in prompt else SECRET

    client = FakeClient({("POST", "/v1/registrations"): (200, {"id": "r1", "discord_user_id": "123",
                                                               "label": "Adam",
                                                               "replaced": False, "withdrawn": 0})})
    out = io.StringIO()
    assert osctl.run(["member", "add", "123", "--label", "Adam"], client, out=out, prompt=fake_getpass) == 0
    assert len(prompts) == 2
    assert client.calls == [("POST", "/v1/registrations", {
        "discord_user_id": "123", "automation_url": URL, "signing_secret": SECRET, "label": "Adam"})]
    assert SECRET not in out.getvalue() and URL not in out.getvalue()
    assert "set the webhook for member 123" in out.getvalue()


def test_register_from_file(tmp_path):
    path = tmp_path / "secrets.txt"
    path.write_text(f"{URL}\n\n{SECRET}\n", encoding="utf-8")
    client = FakeClient({("POST", "/v1/registrations"): (200, {"id": "r1", "discord_user_id": "123",
                                                               "label": None,
                                                               "replaced": True, "withdrawn": 2})})
    out = io.StringIO()
    assert osctl.run(["member", "add", "123", "--from-file", str(path)], client, out=out,
                     prompt=lambda p: pytest.fail("must not prompt")) == 0
    assert client.calls[0][2]["signing_secret"] == SECRET
    assert "replaced the webhook" in out.getvalue() and "2 pending request(s) withdrawn" in out.getvalue()


def test_register_from_file_needs_exactly_two_lines(tmp_path):
    path = tmp_path / "secrets.txt"
    path.write_text(URL, encoding="utf-8")
    with pytest.raises(osctl.OsctlError, match="exactly two lines"):
        osctl.run(["member", "add", "1", "--from-file", str(path)], FakeClient(), out=io.StringIO())


def test_errors_explain_without_echo():
    client = FakeClient({("POST", "/v1/registrations"): (400, {"error": "url_rejected"})})
    with pytest.raises(osctl.OsctlError, match="url_rejected") as excinfo:
        osctl.run(["member", "add", "1"], client, out=io.StringIO(), prompt=lambda p: URL if "URL" in p else SECRET)
    assert URL not in str(excinfo.value)
    client = FakeClient({("GET", "/v1/status"): (401, None)})
    with pytest.raises(osctl.OsctlError, match="bearer token"):
        osctl.run(["bridge", "status"], client, out=io.StringIO())


def test_pause_waits_for_withdrawals():
    client = FakeClient({
        ("POST", "/v1/pause"): (200, {"paused": True, "withdrawn": 2}),
        ("GET", "/v1/status"): [(200, {"paused": True, "pending_final_messages": 2}),
                                (200, {"paused": True, "pending_final_messages": 0})],
    })
    out = io.StringIO()
    assert osctl.run(["bridge", "pause"], client, out=out, sleep=lambda s: None) == 0
    assert "safe to stop" in out.getvalue()


def test_pause_reports_what_is_still_pending():
    client = FakeClient({
        ("POST", "/v1/pause"): (200, {"paused": True, "withdrawn": 1}),
        ("GET", "/v1/status"): (200, {"paused": True, "pending_final_messages": 1}),
    })
    out = io.StringIO()
    assert osctl.run(["bridge", "pause"], client, out=out, sleep=lambda s: None) == 1
    assert "1 withdrawal message(s) still pending" in out.getvalue()


@pytest.mark.parametrize("argv,call", [
    (["member", "remove", "123"], ("DELETE", "/v1/registrations/123")),
    (["bridge", "unpause"], ("DELETE", "/v1/pause")),
    (["bridge", "status"], ("GET", "/v1/status")),
])
def test_command_routes(argv, call):
    client = FakeClient({("DELETE", "/v1/registrations/123"): (200, {"revoked": True, "withdrawn": 0}),
                         ("GET", "/v1/status"): (200, {"paused": False, "pending_final_messages": 0})})
    assert osctl.run(argv, client, out=io.StringIO()) == 0
    assert client.calls[0][:2] == call


def test_load_token(tmp_path):
    assert osctl.load_token({"BRIDGE_REGISTRATION_TOKEN": " abc "}) == "abc"
    path = tmp_path / "t"
    path.write_text("filetoken\n", encoding="utf-8")
    assert osctl.load_token({"BRIDGE_REGISTRATION_TOKEN_FILE": str(path)}) == "filetoken"
    with pytest.raises(osctl.OsctlError):
        osctl.load_token({})


def test_mcp_client_prints_the_connector_form_once():
    form = {"client_id": "osc_abc", "client_secret": "oss_secret", "mcp_server_url": "https://b.example/mcp",
            "authorization_endpoint": "https://b.example/oauth/authorize",
            "token_endpoint": "https://b.example/oauth/token", "scopes": "mcp",
            "token_auth_method": "client_secret_post"}
    client = FakeClient({("POST", "/v1/registrations/123/mcp-clients"): (200, form)})
    out = io.StringIO()
    assert osctl.run(["connector", "client", "add", "123"], client, out=out) == 0
    text = out.getvalue()
    for value in form.values():
        assert value in text
    assert "shown only now" in text


@pytest.mark.parametrize("argv,call", [
    (["connector", "list", "123"], ("GET", "/v1/registrations/123/mcp-connections")),
    (["connector", "disconnect", "123"], ("DELETE", "/v1/registrations/123/mcp-connections")),
    (["connector", "disconnect", "123", "con_1"], ("DELETE", "/v1/registrations/123/mcp-connections/con_1")),
])
def test_connection_commands(argv, call):
    client = FakeClient({("GET", call[1]): (200, [{"connection_id": "con_1", "client_kind": "public",
                                                  "created_at": 1, "last_used_at": None}]),
                         ("DELETE", call[1]): (200, {"disconnected": 1})})
    assert osctl.run(argv, client, out=io.StringIO()) == 0
    assert client.calls[0][:2] == call


@pytest.mark.parametrize("command", [["member", "add"], ["member", "remove"], ["webhook", "set"],
                                     ["webhook", "remove"], ["connector", "client", "add"],
                                     ["connector", "client", "list"], ["connector", "list"],
                                     ["connector", "disconnect"]])
def test_a_username_is_refused_before_any_request(command, capsys):
    """PR 8a pilot: `disconnect-mcp superultramegadeathagon` got an opaque 404. Now it never leaves osctl."""
    client = FakeClient()
    with pytest.raises(SystemExit):
        osctl.run([*command, "someusername"], client, out=io.StringIO(), prompt=lambda p: pytest.fail("no prompt"))
    assert client.calls == []
    assert "not a Discord user id" in capsys.readouterr().err


def test_help_names_the_argument_discord_user_id(capsys):
    with pytest.raises(SystemExit):
        osctl.run(["connector", "disconnect", "--help"], FakeClient(), out=io.StringIO())
    assert "DISCORD_USER_ID" in capsys.readouterr().out


def test_404_for_a_user_id_says_there_is_no_registration():
    client = FakeClient({("GET", "/v1/registrations/123/mcp-connections"): (404, {"error": "not_found"})})
    with pytest.raises(osctl.OsctlError, match="no active membership for that Discord user id"):
        osctl.run(["connector", "list", "123"], client, out=io.StringIO())


def test_connector_settings_prints_every_value_and_the_redirect():
    form = {"connector_name": "Outer Steppes", "mcp_server_url": "https://b.example/mcp", "client_id": "outer-steppes",
            "client_secret": "", "authorization_endpoint": "https://b.example/oauth/authorize",
            "token_endpoint": "https://b.example/oauth/token", "scopes": "mcp", "token_auth_method": "none",
            "discord_redirect_url": "https://b.example/oauth/discord/callback"}
    client = FakeClient({("GET", "/v1/connector-settings"): (200, form)})
    out = io.StringIO()
    assert osctl.run(["connector", "settings"], client, out=out) == 0
    text = out.getvalue()
    for key, value in form.items():
        assert value in text or key == "client_secret"
    assert "(leave empty)" in text and "None of these are secrets" in text


@pytest.mark.parametrize("argv,call", [
    (["connector", "client", "list", "123"], ("GET", "/v1/registrations/123/mcp-clients")),
    (["connector", "client", "delete", "osc_abc"], ("DELETE", "/v1/mcp-clients/osc_abc")),
])
def test_mcp_client_management_routes(argv, call):
    client = FakeClient({("GET", "/v1/registrations/123/mcp-clients"): (200, [])})
    assert osctl.run(argv, client, out=io.StringIO()) == 0
    assert client.calls[0][:2] == call


def test_webhook_remove_says_the_member_and_connector_stay():
    client = FakeClient({("DELETE", "/v1/registrations/123/automation"): (200, {"removed": True, "withdrawn": 1})})
    out = io.StringIO()
    assert osctl.run(["webhook", "remove", "123"], client, out=out) == 0
    assert client.calls == [("DELETE", "/v1/registrations/123/automation", None)]
    assert "removed the webhook; 1 pending request(s) withdrawn" in out.getvalue()
    assert "member and their grok.com connector stay" in out.getvalue()


def test_webhook_remove_with_nothing_set():
    client = FakeClient({("DELETE", "/v1/registrations/123/automation"): (200, {"removed": False, "withdrawn": 0})})
    out = io.StringIO()
    assert osctl.run(["webhook", "remove", "123"], client, out=out) == 0
    assert "no webhook set" in out.getvalue()


def test_webhook_remove_for_a_non_member():
    client = FakeClient({("DELETE", "/v1/registrations/123/automation"): (404, {"error": "not_found"})})
    with pytest.raises(osctl.OsctlError, match="no active membership"):
        osctl.run(["webhook", "remove", "123"], client, out=io.StringIO())


def test_members_lists_each_member_and_a_count():
    client = FakeClient({("GET", "/v1/registrations"): (200, [
        {"id": "r1", "discord_user_id": "123", "label": "alice", "created_at": 1, "automation": "grok_webhook",
         "secret_banned": False, "connections": 2, "last_used_at": 9},
        {"id": "r2", "discord_user_id": "456", "label": None, "created_at": 2, "automation": None,
         "secret_banned": False, "connections": 0, "last_used_at": None},
        {"id": "r3", "discord_user_id": "789", "label": None, "created_at": 3, "automation": "grok_webhook",
         "secret_banned": True, "connections": 1, "last_used_at": None},
    ])})
    out = io.StringIO()
    assert osctl.run(["member", "list"], client, out=out) == 0
    assert client.calls == [("GET", "/v1/registrations", None)]
    lines = out.getvalue().splitlines()
    # The membership id is what log lines carry (DESIGN.md, Another member's connector).
    assert lines[0] == "123  membership r1  alice  joined 1  grok_webhook  2 connection(s), last used 9"
    assert lines[1] == "456  membership r2  -  joined 2  no webhook  0 connection(s), never used"
    assert lines[2] == "789  membership r3  -  joined 3  grok_webhook (secret banned)  1 connection(s), never used"
    assert lines[3] == "3 member(s)"


@pytest.mark.parametrize("code,hint", [("secret_banned", "rotate it in grok.com"),
                                       ("url_in_use", "create a new automation")])
def test_a_save_refusal_says_what_to_do(tmp_path, code, hint):
    path = tmp_path / "secrets.txt"
    path.write_text(f"{URL}\n\n{SECRET}\n", encoding="utf-8")
    client = FakeClient({("POST", "/v1/registrations"): (400, {"error": code})})
    with pytest.raises(osctl.OsctlError, match=f"HTTP 400: {code}: .*{hint}"):
        osctl.run(["member", "add", "123", "--from-file", str(path)], client, out=io.StringIO())


def test_members_with_none():
    out = io.StringIO()
    assert osctl.run(["member", "list"], FakeClient({("GET", "/v1/registrations"): (200, [])}), out=out) == 0
    assert out.getvalue() == "0 member(s)\n"


# -- webhook set ------------------------------------------------------------------

MEMBER = {"discord_user_id": "123", "label": None, "created_at": 1, "automation": None,
          "connections": 0, "last_used_at": None}


def test_webhook_set_replaces_an_existing_members_webhook():
    client = FakeClient({("GET", "/v1/registrations"): (200, [MEMBER]),
                         ("POST", "/v1/registrations"): (200, {"id": "r1", "discord_user_id": "123",
                                                               "replaced": True, "withdrawn": 1})})
    out = io.StringIO()
    assert osctl.run(["webhook", "set", "123"], client, out=out,
                     prompt=lambda p: URL if "URL" in p else SECRET) == 0
    assert client.calls == [("GET", "/v1/registrations", None), ("POST", "/v1/registrations", {
        "discord_user_id": "123", "automation_url": URL, "signing_secret": SECRET})]
    assert "replaced the webhook for member 123" in out.getvalue()


def test_webhook_set_never_adds_a_member():
    """A mistyped id is refused before the prompt, instead of becoming a member."""
    client = FakeClient({("GET", "/v1/registrations"): (200, [MEMBER])})
    with pytest.raises(osctl.OsctlError, match="no active membership.*member add"):
        osctl.run(["webhook", "set", "124"], client, out=io.StringIO(), prompt=lambda p: pytest.fail("no prompt"))
    assert client.calls == [("GET", "/v1/registrations", None)]


def test_webhook_set_takes_no_label():
    with pytest.raises(SystemExit):
        osctl.build_parser().parse_args(["webhook", "set", "123", "--label", "x"])


# -- routing (DESIGN.md, Registration (operator): Where it runs) -------------------------------------

class Loader:
    """Stands in for load_script: records each name asked for and answers from `modules`.
    A name not given is None, as inside the image."""

    def __init__(self, **modules):
        self.modules = modules
        self.asked = []

    def __call__(self, name):
        self.asked.append(name)
        return self.modules.get(name)


class ForwardRecorder:
    """An osctl_container.py module whose Dev records what it would forward with podman exec."""

    class DevError(Exception):
        pass

    def __init__(self):
        self.forwarded = []
        recorder = self

        class Dev:
            def __init__(self, env=None):
                pass

            def osctl(self, args):
                recorder.forwarded.append(args)
                return 0
        self.Dev = Dev


# A bearer that can't be read: any path that reads it fails with code 2.
UNREADABLE_BEARER = {"BRIDGE_REGISTRATION_TOKEN_FILE": "/made-up/missing/registration-token"}


@pytest.fixture
def direct(monkeypatch):
    """main() with OSCTL_ADMIN_URL set: records each AdminClient it builds."""
    made = []

    def client(base_url, token):
        fake = FakeClient({("GET", "/v1/status"): (200, {"paused": False, "pending_final_messages": 0})})
        made.append((base_url, token, fake))
        return fake
    monkeypatch.setattr(osctl, "AdminClient", client)
    return made


def test_with_osctl_admin_url_an_api_group_calls_the_listener_directly(direct):
    load = Loader()
    env = {"OSCTL_ADMIN_URL": "http://127.0.0.1:8081", "BRIDGE_REGISTRATION_TOKEN": "made-up-bearer"}
    assert osctl.main(["bridge", "status"], env=env, load=load) == 0
    [(base, token, client)] = direct
    assert (base, token) == ("http://127.0.0.1:8081", "made-up-bearer")
    assert client.calls == [("GET", "/v1/status", None)]
    assert load.asked == []  # no osctl_container.py, no podman


@pytest.mark.parametrize("admin_url", [None, "", "   "])
def test_without_osctl_admin_url_an_api_group_is_forwarded_and_reads_no_bearer(admin_url, direct):
    env = dict(UNREADABLE_BEARER)
    if admin_url is not None:
        env["OSCTL_ADMIN_URL"] = admin_url
    dev = ForwardRecorder()
    load = Loader(osctl_container=dev)
    assert osctl.main(["member", "add", "123", "--label", "me"], env=env, load=load) == 0
    assert dev.forwarded == [["member", "add", "123", "--label", "me"]]
    assert load.asked == ["osctl_container"] and direct == []


def test_the_real_forward_runs_podman_exec_with_the_same_arguments(capsys):
    dev = osctl.load_script("osctl_container")
    calls = []
    real_dev = dev.Dev

    def runner(cmd, capture=False):
        calls.append(cmd)
        return 0, ""
    dev.Dev = lambda env=None: real_dev(runner=runner, env=env, interactive=lambda: False)
    assert osctl.main(["connector", "list", "123"], env={}, load=lambda name: dev) == 0
    assert calls == [["podman", "exec", "-i", "outer-steppes", "osctl", "connector", "list", "123"]]
    assert "running in container outer-steppes" in capsys.readouterr().err


@pytest.mark.parametrize("argv", [["member", "add", "123", "--from-file", "made-up.txt"],
                                  ["webhook", "set", "123", "--from-file", "made-up.txt"]])
def test_from_file_is_refused_when_forwarded(argv, capsys):
    load = Loader(osctl_container=ForwardRecorder())
    assert osctl.main(argv, env={}, load=load) == 2
    err = capsys.readouterr().err
    assert "--from-file can't be forwarded" in err and "stdin" in err
    assert load.asked == []


def test_inside_the_image_an_empty_osctl_admin_url_stops_with_a_message(capsys):
    assert osctl.main(["bridge", "status"], env={"OSCTL_ADMIN_URL": ""}, load=Loader()) == 2
    assert "OSCTL_ADMIN_URL is unset or empty" in capsys.readouterr().err


@pytest.mark.parametrize("argv,name", [(["container", "up"], "osctl_container"),
                                       (["command", "register", "--dry-run"], "osctl_command")])
def test_host_only_groups_stop_inside_the_image(argv, name, capsys):
    assert osctl.main(argv, env={"OSCTL_ADMIN_URL": "http://127.0.0.1:8081"}, load=Loader()) == 2
    err = capsys.readouterr().err
    assert "runs on the host" in err and f"scripts/{name}.py" in err


class CommandRecorder:
    def __init__(self):
        self.argv = None

    def main(self, argv):
        self.argv = argv
        return 0


@pytest.mark.parametrize("admin_url", [None, "http://127.0.0.1:8081"])
@pytest.mark.parametrize("argv,passed", [
    (["command", "register"], []),
    (["command", "register", "--dry-run", "--env-file", "x.env"], ["--dry-run", "--env-file", "x.env"]),
    (["command", "list"], ["--list"]),
    (["command", "list", "--guild", "3", "--guild", "4"], ["--list", "--guild", "3", "--guild", "4"]),
    (["command", "clear-guild"], ["--clear-guild"]),
    (["command", "clear-guild", "--guild", "3", "--guild", "4", "--env-file", "x.env"],
     ["--clear-guild", "--guild", "3", "--guild", "4", "--env-file", "x.env"]),
    (["command", "guilds"], ["--guilds"]),
    (["command", "get", "/users/@me/guilds?limit=5"], ["--get", "/users/@me/guilds?limit=5"]),
])
def test_command_always_runs_here_and_reads_no_bearer(argv, passed, admin_url, direct):
    """Never through podman exec: the bot token is in this shell and must never enter the container."""
    env = dict(UNREADABLE_BEARER)
    if admin_url:
        env["OSCTL_ADMIN_URL"] = admin_url
    commands, dev = CommandRecorder(), ForwardRecorder()
    load = Loader(osctl_command=commands, osctl_container=dev)
    assert osctl.main(argv, env=env, load=load) == 0
    assert commands.argv == passed
    assert load.asked == ["osctl_command"] and dev.forwarded == [] and direct == []


@pytest.mark.parametrize("argv", [["command", "list", "--dry-run"], ["command", "register", "--guild", "1"],
                                  ["command", "register", "--clear-guild-commands"],
                                  ["command", "register", "--list"],
                                  ["command", "register", "--clear-guild"], ["command", "list", "--clear-guild"],
                                  ["command", "clear-guild", "--dry-run"], ["command", "clear-guild", "/a"],
                                  ["command", "guilds", "--guild", "1"], ["command", "guilds", "--env-file", "x.env"],
                                  ["command", "get"], ["command", "get", "/a", "--env-file", "x.env"]])
def test_command_takes_only_its_own_flags(argv):
    with pytest.raises(SystemExit):
        osctl.main(argv, env={}, load=Loader(osctl_command=CommandRecorder()))


def test_container_hands_everything_after_it_to_osctl_container_py():
    seen = []

    class Dev:
        @staticmethod
        def main(argv, prog):
            seen.append((argv, prog))
            return 0
    load = Loader(osctl_container=Dev)
    assert osctl.main(["container", "up", "--env-file", "x.env", "--build"], env=dict(UNREADABLE_BEARER),
                      load=load) == 0
    assert seen == [(["up", "--env-file", "x.env", "--build"], "osctl container")]
    assert load.asked == ["osctl_container"]


# -- one module per run (ADR-0008, Consequences) ------------------------------------

@pytest.fixture
def loaded_scripts(monkeypatch):
    """The file name of every source file executed as a module from now on, by any import route."""
    seen = []
    real = importlib.machinery.SourceFileLoader.exec_module

    def record(self, module):
        seen.append(Path(self.path).name)
        return real(self, module)
    monkeypatch.setattr(importlib.machinery.SourceFileLoader, "exec_module", record)
    return seen


def fresh_osctl():
    """osctl.py loaded anew, so its own top-level imports are recorded too."""
    fresh_spec = importlib.util.spec_from_file_location("osctl_fresh", SCRIPT)
    module = importlib.util.module_from_spec(fresh_spec)
    fresh_spec.loader.exec_module(module)
    return module


def test_a_command_run_loads_osctl_command_and_never_osctl_container(loaded_scripts, capsys):
    assert fresh_osctl().main(["command", "register", "--dry-run"], env={}) == 0
    assert '"name":"ask"' in capsys.readouterr().out  # osctl_command.py really ran
    assert "osctl_command.py" in loaded_scripts and "osctl_container.py" not in loaded_scripts


def test_a_container_run_loads_osctl_container_and_never_osctl_command(loaded_scripts, capsys):
    with pytest.raises(SystemExit):
        fresh_osctl().main(["container", "--help"], env={})
    assert "osctl container" in capsys.readouterr().out
    assert "osctl_container.py" in loaded_scripts and "osctl_command.py" not in loaded_scripts


def test_a_forwarded_run_loads_osctl_container_and_never_osctl_command(loaded_scripts, monkeypatch):
    ran = []

    def fake_run(cmd, **kwargs):
        ran.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")
    monkeypatch.setattr(subprocess, "run", fake_run)  # no real podman
    assert fresh_osctl().main(["bridge", "status"], env={}) == 0
    assert ran and ran[-1][:2] == ["podman", "exec"]
    assert "osctl_container.py" in loaded_scripts and "osctl_command.py" not in loaded_scripts


# -- the image ---------------------------------------------------------------------

def test_the_image_copies_nothing_from_scripts_but_osctl():
    """osctl_container.py and osctl_command.py stay out of the image, so the bot-token code never ships."""
    repo = SCRIPT.parents[1]
    sources = []
    for line in (repo / "deploy" / "Dockerfile").read_text(encoding="utf-8").splitlines():
        words = line.split()
        if words[:1] in (["COPY"], ["ADD"]):
            sources += [w for w in words[1:-1] if not w.startswith("--")]
    assert sources  # the parse found the COPY lines
    for source in sources:
        assert source.rstrip("/") not in (".", "scripts") and "*" not in source, source
    assert [s for s in sources if s.startswith("scripts")] == ["scripts/osctl.py"]
    assert "scripts/osctl_command.py" in (repo / ".dockerignore").read_text(encoding="utf-8").split()


def test_the_image_osctl_is_the_script_not_the_launcher():
    """`pip install .` makes /usr/local/bin/osctl from the console-script entry, and the launcher
    finds no scripts/ in the image. The COPY after it puts osctl.py there instead."""
    lines = (SCRIPT.parents[1] / "deploy" / "Dockerfile").read_text(encoding="utf-8").splitlines()
    installs = [i for i, line in enumerate(lines) if "pip install" in line and not line.startswith("#")]
    copy = lines.index("COPY scripts/osctl.py /usr/local/bin/osctl")
    assert installs and max(installs) < copy


# -- the osctl command in the venv (DESIGN.md, Registration (operator)) ------------------------------------------

def test_the_console_script_points_at_the_launcher():
    pyproject = (SCRIPT.parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    assert 'osctl = "outer_steppes.osctl_launcher:main"' in pyproject


def test_the_launcher_runs_the_checkouts_osctl_and_loads_no_server_code():
    """`osctl command` holds the bot token: in its process, nothing from the server package but the
    launcher and the package's empty __init__ may be loaded (ADR-0008)."""
    repo = SCRIPT.parents[1]
    probe = ("import sys\n"
             "sys.argv = ['osctl', 'command', 'register', '--dry-run']\n"
             "from outer_steppes import osctl_launcher\n"
             "code = osctl_launcher.main()\n"
             "print('MODULES', sorted(m for m in sys.modules if m.startswith('outer_steppes')))\n"
             "sys.exit(code)\n")
    env = {**os.environ, "PYTHONPATH": str(repo / "src")}
    done = subprocess.run([sys.executable, "-c", probe], cwd=repo, env=env, capture_output=True, text=True,
                          timeout=60)
    assert done.returncode == 0, done.stderr
    assert '"name":"ask"' in done.stdout  # scripts/osctl.py ran, and handed over to osctl_command.py
    assert "MODULES ['outer_steppes', 'outer_steppes.osctl_launcher']" in done.stdout


def test_the_launcher_outside_a_checkout_says_where_osctl_runs(monkeypatch, tmp_path, capsys):
    from outer_steppes import osctl_launcher
    monkeypatch.setattr(osctl_launcher, "SCRIPT", tmp_path / "scripts" / "osctl.py")
    assert osctl_launcher.main() == 2
    assert "pip install -e ." in capsys.readouterr().err


def test_the_launcher_passes_its_arguments_through(monkeypatch, capsys):
    from outer_steppes import osctl_launcher
    monkeypatch.setattr(sys, "argv", ["osctl", "member", "made-up-verb"])
    with pytest.raises(SystemExit) as excinfo:
        osctl_launcher.main()
    assert excinfo.value.code == 2
    assert "made-up-verb" in capsys.readouterr().err


# ------------------------------------------------------------------ bridge claim (DESIGN.md, Claiming the bridge)

def test_bridge_claim_prints_the_link_and_when_it_expires():
    link = "https://bridge.example/account/claim?token=made-up-claim-token"
    client = FakeClient({("POST", "/v1/claim"): (200, {"claim_url": link, "expires_at": 1_700_003_600})})
    out = io.StringIO()
    assert osctl.run(["bridge", "claim"], client, out=out) == 0
    assert client.calls == [("POST", "/v1/claim", None)]
    text = out.getvalue()
    assert f"  {link}\n" in text and "2023-11-14 23:13 UTC" in text
    assert "replaces any earlier link" in text and "don't paste it anywhere else" in text


@pytest.mark.parametrize("status,error,phrase", [
    (409, "claimed", "already has a member"),
    (409, "operator_mode", "MEMBERSHIP=operator"),
    (404, "discord_login_off", "Discord login is off"),
    (403, None, "paused"),
])
def test_bridge_claim_refusals_say_why(status, error, phrase):
    client = FakeClient({("POST", "/v1/claim"): (status, {"error": error} if error else None)})
    with pytest.raises(osctl.OsctlError, match=phrase):
        osctl.run(["bridge", "claim"], client, out=io.StringIO())


@pytest.mark.parametrize("status,error", [(409, "claimed"), (409, "operator_mode"), (404, "discord_login_off")])
def test_bridge_claim_if_unclaimed_is_quiet_when_nothing_needs_claiming(status, error):
    """What `osctl container up` runs on every start."""
    client = FakeClient({("POST", "/v1/claim"): (status, {"error": error})})
    out = io.StringIO()
    assert osctl.run(["bridge", "claim", "--if-unclaimed"], client, out=out) == 0
    assert out.getvalue() == ""


@pytest.mark.parametrize("status,error,phrase", [
    (403, None, "paused"),
    (401, None, "bearer"),
    (409, "made-up-reason", "HTTP 409"),
    (404, "not_found", "HTTP 404"),
])
def test_bridge_claim_if_unclaimed_still_reports_failures(status, error, phrase):
    client = FakeClient({("POST", "/v1/claim"): (status, {"error": error} if error else None)})
    with pytest.raises(osctl.OsctlError, match=phrase):
        osctl.run(["bridge", "claim", "--if-unclaimed"], client, out=io.StringIO())


def test_bridge_claim_if_unclaimed_prints_the_link_on_an_unclaimed_bridge():
    link = "https://bridge.example/account/claim?token=made-up-claim-token"
    client = FakeClient({("POST", "/v1/claim"): (200, {"claim_url": link, "expires_at": 1_700_003_600})})
    out = io.StringIO()
    assert osctl.run(["bridge", "claim", "--if-unclaimed"], client, out=out) == 0
    assert f"  {link}\n" in out.getvalue()
