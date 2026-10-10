"""The command registry must match DESIGN.md, Commands, byte for byte."""

from __future__ import annotations

import importlib.util
import http.client
import json
import urllib.error
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "osctl_command.py"
spec = importlib.util.spec_from_file_location("osctl_command", SCRIPT)
osctl_command = importlib.util.module_from_spec(spec)
spec.loader.exec_module(osctl_command)

EVERYWHERE = '"type":1,"integration_types":[0,1],"contexts":[0,1,2]'
ASK = ('{"name":"ask",' + EVERYWHERE + ','
       '"description":"Ask your grok.com automation",'
       '"options":[{"name":"prompt","description":"What to ask","type":3,"required":true,'
       '"min_length":1,"max_length":4000},'
       '{"name":"private","description":"Only you see the answer (default: your /settings, private)",'
       '"type":5,"required":false}]}')
STATUS = '{"name":"status",' + EVERYWHERE + ',"description":"Show whether /ask will work for you here"}'
SETTINGS = ('{"name":"settings",' + EVERYWHERE + ','
            '"description":"Show or change whether your answers are private by default",'
            '"options":[{"name":"private","description":"true: only you see answers; false: everyone in the chat does",'
            '"type":5,"required":false}]}')
WEBHOOK = ('{"name":"webhook",' + EVERYWHERE + ',"description":"Your grok.com automation webhook",'
           '"options":[{"name":"set","description":"Get a link to set or replace your webhook (never paste secrets in Discord)","type":1},'
           '{"name":"status","description":"Whether your webhook and grok.com connector are set up","type":1},'
           '{"name":"remove","description":"Remove your webhook; you stay a member","type":1}]}')
LEAVE = ('{"name":"leave",' + EVERYWHERE + ','
         '"description":"Leave Outer Steppes: removes your webhook and grok.com connection",'
         '"options":[{"name":"confirm","description":"True to leave; without it, only explains what would be deleted",'
         '"type":5,"required":false}]}')
ALL = f"[{ASK},{STATUS},{SETTINGS},{WEBHOOK},{LEAVE}]"

TOKEN = "made-up-bot-token.abc123.xyz"
SETTINGS_KEYS = ("DISCORD_APPLICATION_ID", "GUILD_ALLOWLIST", "OS_ENV_FILE", "DISCORD_BOT_TOKEN")


@pytest.fixture(autouse=True)
def isolated(monkeypatch, tmp_path):
    """No test may read the developer's real repo-root .env or shell settings."""
    repo = tmp_path / "repo"
    repo.mkdir()
    monkeypatch.setattr(osctl_command, "REPO", repo)
    monkeypatch.chdir(tmp_path)
    for key in SETTINGS_KEYS:
        monkeypatch.delenv(key, raising=False)
    return repo


def test_commands_are_exact():
    """The content is locked exactly; the whitespace isn't (the body is pretty-printed for --dry-run)."""
    assert json.loads(osctl_command.serialize(osctl_command.build_commands())) == json.loads(ALL)


def test_descriptions_fit_discords_limit():
    for cmd in osctl_command.build_commands():
        for item in [cmd, *cmd.get("options", [])]:
            assert 1 <= len(item["description"]) <= 100


def test_every_command_is_guild_and_user_installable_in_all_contexts():
    for cmd in osctl_command.build_commands():
        assert cmd["integration_types"] == [0, 1]
        assert cmd["contexts"] == [0, 1, 2]
        assert "default_member_permissions" not in cmd and "dm_permission" not in cmd


@pytest.mark.parametrize("pasted", ["abc.def.ghi", "Bot abc.def.ghi", " bot abc.def.ghi ", '"abc.def.ghi"'])
def test_token_normalization(pasted):
    assert osctl_command.normalize_token(pasted) == "abc.def.ghi"


def test_dry_run_prints_json_without_a_token(monkeypatch, capsys):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    assert osctl_command.main(["--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out) == json.loads(ALL)


def test_live_run_requires_token(monkeypatch):
    monkeypatch.delenv("DISCORD_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "1")
    assert osctl_command.main([]) == 2


@pytest.fixture
def calls(monkeypatch):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "Bot secret.token.value")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "1")
    monkeypatch.setenv("GUILD_ALLOWLIST", "2,3")
    recorded = []

    def fake_request(method, path, bot_token, body=None):
        recorded.append((method, path, bot_token, body))
        return 200, json.dumps([{"name": "ask"}, {"name": "leave"}, {"name": "status"}])

    monkeypatch.setattr(osctl_command, "_request", fake_request)
    return recorded


def test_registers_globally_only_by_default(calls):
    assert osctl_command.main([]) == 0
    assert [(m, p) for m, p, _, _ in calls] == [("PUT", "/applications/1/commands")]
    assert calls[0][2] == "secret.token.value"


def test_register_never_touches_per_server_commands(calls, monkeypatch):
    monkeypatch.setenv("GUILD_ALLOWLIST", "2,not-a-server-id")  # register doesn't read it
    assert osctl_command.main([]) == 0
    assert [(m, p) for m, p, _, _ in calls] == [("PUT", "/applications/1/commands")]


def test_guild_flag_overrides_allowlist(calls):
    assert osctl_command.main(["--list", "--guild", "7", "--guild", "8"]) == 0
    assert [(m, p) for m, p, _, _ in calls] == [("GET", "/applications/1/commands"),
                                                ("GET", "/applications/1/guilds/7/commands"),
                                                ("GET", "/applications/1/guilds/8/commands")]


def test_list_without_any_guild_shows_only_global(calls, monkeypatch, capsys):
    monkeypatch.delenv("GUILD_ALLOWLIST")
    assert osctl_command.main(["--list"]) == 0
    assert [p for _, p, _, _ in calls] == ["/applications/1/commands"]
    assert "guild" not in capsys.readouterr().out


@pytest.mark.parametrize("argv", [["--list", "--guild"], ["--list", "--guild", "abc"]])
def test_malformed_guild_flag_is_refused(calls, argv):
    assert osctl_command.main(argv) == 2 and calls == []


# ------------------------------------------------------------------ clear-guild (MALU-253)

@pytest.fixture
def puts(monkeypatch):
    """Every request clear-guild sends; Discord answers a per-server bulk overwrite of [] with []."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "Bot " + TOKEN)
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "1")
    monkeypatch.setenv("GUILD_ALLOWLIST", "2,3")
    recorded = []

    def fake_request(method, path, bot_token, body=None):
        recorded.append((method, path, bot_token, body))
        return 200, "[]" if body == b"[]" else json.dumps([{"name": "ask"}])

    monkeypatch.setattr(osctl_command, "_request", fake_request)
    return recorded


def test_clear_guild_empties_each_allowlisted_server_and_never_the_global_commands(puts, capsys):
    assert osctl_command.main(["--clear-guild"]) == 0
    assert puts == [("PUT", "/applications/1/guilds/2/commands", TOKEN, b"[]"),
                    ("PUT", "/applications/1/guilds/3/commands", TOKEN, b"[]")]
    out, err = capsys.readouterr()
    assert "guild 2: per-server commands cleared (HTTP 200, Discord confirmed: none left)" in out
    assert "guild 3: per-server commands cleared" in out and TOKEN not in out + err


def test_clear_guild_flag_overrides_allowlist(puts):
    assert osctl_command.main(["--clear-guild", "--guild", "7", "--guild", "8"]) == 0
    assert [(m, p, b) for m, p, _, b in puts] == [("PUT", "/applications/1/guilds/7/commands", b"[]"),
                                                  ("PUT", "/applications/1/guilds/8/commands", b"[]")]


def test_clear_guild_without_any_guild_sends_nothing(puts, monkeypatch, capsys):
    monkeypatch.delenv("GUILD_ALLOWLIST")
    assert osctl_command.main(["--clear-guild"]) == 2
    assert puts == [] and "--guild" in capsys.readouterr().err


@pytest.mark.parametrize("guild", ["", "abc", "2/../../commands", "1?x=", "²", "-1"])
def test_clear_guild_refuses_a_guild_that_isnt_a_numeric_id(puts, guild):
    """A guild id is part of the path: anything but ASCII digits could aim the PUT of [] elsewhere."""
    assert osctl_command.main(["--clear-guild", "--guild", "7", "--guild", guild]) == 2
    assert puts == []


@pytest.mark.parametrize("argv", [[], ["--list"], ["--clear-guild"]])
@pytest.mark.parametrize("application_id", ["1/commands?x=", "1/../1", "abc", "²"])
def test_a_non_numeric_application_id_is_refused_before_any_request(puts, monkeypatch, capsys, argv, application_id):
    """"1/commands?x=" would turn a per-server PUT of [] into one that empties the global commands."""
    monkeypatch.setenv("DISCORD_APPLICATION_ID", application_id)
    assert osctl_command.main(argv) == 2
    assert puts == [] and "numeric application id" in capsys.readouterr().err


def test_clear_guild_goes_on_after_a_failure_and_fails(puts, monkeypatch, capsys):
    def fake_request(method, path, bot_token, body=None):
        puts.append((method, path, bot_token, body))
        return (403, '{"message": "Missing Access", "code": 50001}') if "/guilds/2/" in path else (200, "[]")

    monkeypatch.setattr(osctl_command, "_request", fake_request)
    assert osctl_command.main(["--clear-guild"]) == 1
    assert [p for _, p, _, _ in puts] == ["/applications/1/guilds/2/commands", "/applications/1/guilds/3/commands"]
    out, err = capsys.readouterr()
    assert "guild 2: HTTP 403" in out and "Missing Access" in err
    assert "guild 2: per-server commands cleared" not in out and "guild 3: per-server commands cleared" in out and TOKEN not in out + err


@pytest.mark.parametrize("answer", ['[{"name": "ask"}]', "not json", "{}"])
def test_clear_guild_fails_unless_discord_answers_with_an_empty_list(puts, monkeypatch, capsys, answer):
    monkeypatch.setattr(osctl_command, "_request", lambda *a, **k: (200, answer))
    assert osctl_command.main(["--clear-guild", "--guild", "7"]) == 1
    out = capsys.readouterr().out
    assert "osctl command list --guild 7" in out
    assert "cleared" not in out and "confirmed" not in out


@pytest.mark.parametrize("other", [["--dry-run"], ["--list"], ["--guilds"], ["--get", "/users/@me"]])
def test_clear_guild_runs_only_on_its_own(puts, capsys, other):
    """Run directly, main picks a verb by flag: a dry run or a read must never clear, nor a clear read."""
    assert osctl_command.main(["--clear-guild", *other]) == 2
    assert osctl_command.main([*other, "--clear-guild"]) == 2
    out, err = capsys.readouterr()
    assert puts == [] and out == "" and "--clear-guild runs on its own" in err


@pytest.mark.parametrize("error", [urllib.error.URLError("made-up name not resolved"), TimeoutError("timed out"),
                                   ConnectionRefusedError(10061, "refused"), http.client.IncompleteRead(b"[")])
def test_clear_guild_goes_on_after_a_network_error_and_fails(puts, monkeypatch, capsys, error):
    def fake_request(method, path, bot_token, body=None):
        puts.append((method, path, bot_token, body))
        if "/guilds/2/" in path:
            raise error
        return 200, "[]"

    monkeypatch.setattr(osctl_command, "_request", fake_request)
    assert osctl_command.main(["--clear-guild"]) == 1
    assert [p for _, p, _, _ in puts] == ["/applications/1/guilds/2/commands", "/applications/1/guilds/3/commands"]
    out, err = capsys.readouterr()
    assert "guild 2: request failed:" in out and "guild 2: per-server commands cleared" not in out
    assert "guild 3: per-server commands cleared" in out and TOKEN not in out + err


def test_list_shows_global_and_guild(calls, capsys):
    assert osctl_command.main(["--list"]) == 0
    out = capsys.readouterr().out
    assert "global: ask, leave, status" in out and "guild 2:" in out and "guild 3:" in out


def test_401_prints_discord_error_and_hint_without_the_token(monkeypatch, capsys):
    monkeypatch.setenv("DISCORD_BOT_TOKEN", "Bot secret.token.value")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "1")
    monkeypatch.setattr(osctl_command, "_request",
                        lambda *a, **k: (401, '{"message": "401: Unauthorized", "code": 0}'))
    assert osctl_command.main([]) == 1
    out, err = capsys.readouterr()
    assert "401: Unauthorized" in err and "Reset Token" in err
    assert "secret.token.value" not in out + err


# ------------------------------------------------------------------ env file (MALU-73)

@pytest.fixture
def sent(monkeypatch):
    """Every request main would send; the bot token comes from the shell, as it must."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", TOKEN)
    recorded = []

    def fake_request(method, path, bot_token, body=None):
        recorded.append((method, path, bot_token))
        return 200, "[]"

    monkeypatch.setattr(osctl_command, "_request", fake_request)
    return recorded


def write_env(path, text, encoding="utf-8"):
    path.write_bytes(text.encode(encoding))
    return path


def test_the_repo_root_dot_env_supplies_the_application_id_and_guilds(sent, isolated, capsys):
    write_env(isolated / ".env", "DISCORD_APPLICATION_ID=11\nGUILD_ALLOWLIST=2,3\nDISCORD_PUBLIC_KEY=made-up-key\n")
    assert osctl_command.main(["--list"]) == 0
    assert [p for _, p, _ in sent] == ["/applications/11/commands", "/applications/11/guilds/2/commands",
                                       "/applications/11/guilds/3/commands"]
    out, err = capsys.readouterr()
    assert err.strip() == f"env file: {isolated / '.env'}"  # the path only
    assert "made-up-key" not in out + err and "DISCORD_APPLICATION_ID=" not in err


def test_the_shell_wins_over_the_file_for_each_key(sent, isolated, monkeypatch):
    write_env(isolated / ".env", "DISCORD_APPLICATION_ID=11\nGUILD_ALLOWLIST=2\n")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "99")
    assert osctl_command.main(["--list"]) == 0
    assert [p for _, p, _ in sent] == ["/applications/99/commands", "/applications/99/guilds/2/commands"]


def test_an_empty_shell_variable_falls_back_to_the_file(sent, isolated, monkeypatch):
    write_env(isolated / ".env", "DISCORD_APPLICATION_ID=11\n")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "  ")
    assert osctl_command.main([]) == 0
    assert sent[0][1] == "/applications/11/commands"


def test_an_explicit_env_file_beats_os_env_file_and_the_repo_dot_env(sent, isolated, tmp_path, monkeypatch, capsys):
    write_env(isolated / ".env", "DISCORD_APPLICATION_ID=1\n")
    write_env(tmp_path / "from_env_var.env", "DISCORD_APPLICATION_ID=2\n")
    write_env(tmp_path / "given.env", "DISCORD_APPLICATION_ID=3\n")
    monkeypatch.setenv("OS_ENV_FILE", str(tmp_path / "from_env_var.env"))
    assert osctl_command.main(["--env-file", str(tmp_path / "given.env")]) == 0
    assert osctl_command.main([]) == 0
    monkeypatch.delenv("OS_ENV_FILE")
    assert osctl_command.main([]) == 0
    assert [p for _, p, _ in sent] == ["/applications/3/commands", "/applications/2/commands",
                                       "/applications/1/commands"]
    assert str(tmp_path / "given.env") not in capsys.readouterr().err  # an explicit path isn't echoed


@pytest.mark.parametrize("argv", [["--env-file", "nope.env"], ["--env-file"], ["--env-file", " "]])
def test_a_missing_or_empty_explicit_env_file_is_refused(sent, argv, capsys):
    assert osctl_command.main(argv) == 2 and sent == []
    err = capsys.readouterr().err
    assert "env file" in err or "--env-file" in err


def test_a_missing_os_env_file_is_refused(sent, monkeypatch):
    monkeypatch.setenv("OS_ENV_FILE", "nope.env")
    assert osctl_command.main([]) == 2 and sent == []


def test_a_dot_env_in_the_current_directory_is_never_used(sent, tmp_path):
    """Only the repo root is searched: another project's .env must not be read from the cwd."""
    write_env(tmp_path / ".env", "DISCORD_APPLICATION_ID=11\n")
    assert osctl_command.main([]) == 2 and sent == []


@pytest.mark.parametrize("line", ["DISCORD_BOT_TOKEN=made-up-file-token", "DISCORD_BOT_TOKEN=",
                                  "  DISCORD_BOT_TOKEN = made-up-file-token", "DISCORD_BOT_TOKEN"])
def test_a_file_that_names_the_bot_token_is_refused_without_printing_it(sent, isolated, capsys, line):
    write_env(isolated / ".env", f"DISCORD_APPLICATION_ID=11\n{line}\n")
    assert osctl_command.main([]) == 2 and sent == []
    out, err = capsys.readouterr()
    assert "DISCORD_BOT_TOKEN" in err and "made-up-file-token" not in out + err


def test_the_token_is_never_taken_from_a_file(monkeypatch, isolated, capsys):
    """With no shell token, the file can't supply one: the refusal comes first, and nothing is sent."""
    monkeypatch.setattr(osctl_command, "_request", lambda *a, **k: pytest.fail("sent a request"))
    write_env(isolated / ".env", "DISCORD_APPLICATION_ID=11\nDISCORD_BOT_TOKEN=made-up-file-token\n")
    assert osctl_command.main([]) == 2
    write_env(isolated / ".env", "DISCORD_APPLICATION_ID=11\n")
    assert osctl_command.main([]) == 2  # no token anywhere: still required from the shell
    assert "made-up-file-token" not in "".join(capsys.readouterr())


def test_nothing_from_the_file_is_printed(sent, isolated, capsys):
    write_env(isolated / ".env", "DISCORD_APPLICATION_ID=11\nDISCORD_CLIENT_SECRET=made-up-client-secret\n"
                                 "DISCORD_PUBLIC_KEY=made-up-public-key\n")
    assert osctl_command.main(["--list"]) == 0
    out, err = capsys.readouterr()
    assert "made-up-client-secret" not in out + err and "made-up-public-key" not in out + err
    assert TOKEN not in out + err


def test_parsing_matches_podman_env_files(sent, isolated):
    """Comments and blank lines are skipped, the last value wins, quotes are kept and a bare key has no value, as
    `podman --env-file` does. A BOM is read past here; osctl_container.py refuses it, because Podman keeps it
    in the first key."""
    write_env(isolated / ".env", "# settings\n\nDISCORD_APPLICATION_ID=1\nDISCORD_APPLICATION_ID=22\n"
                                 "GUILD_ALLOWLIST\n", encoding="utf-8-sig")
    assert osctl_command.main(["--list"]) == 0
    assert [p for _, p, _ in sent] == ["/applications/22/commands"]  # a bare key gives no guilds
    assert osctl_command.load_settings([], {})["DISCORD_APPLICATION_ID"] == "22"
    write_env(isolated / ".env", "DISCORD_APPLICATION_ID=\"22\"\n")
    assert osctl_command.load_settings([], {})["DISCORD_APPLICATION_ID"] == '"22"'


def test_a_bare_key_does_not_hide_the_shell_value(sent, isolated, monkeypatch):
    write_env(isolated / ".env", "DISCORD_APPLICATION_ID\n")
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "5")
    assert osctl_command.main([]) == 0 and sent[0][1] == "/applications/5/commands"


def test_a_utf16_env_file_is_a_clean_error(sent, isolated, capsys):
    write_env(isolated / ".env", "DISCORD_APPLICATION_ID=11\n", encoding="utf-16")
    assert osctl_command.main([]) == 2 and sent == []
    assert "UTF-8" in capsys.readouterr().err


def test_a_directory_named_dot_env_is_not_an_env_file(sent, isolated, capsys):
    (isolated / ".env").mkdir()  # is_file() is False, so nothing is found and the application id is missing
    assert osctl_command.main([]) == 2 and sent == []


def test_dry_run_reads_no_env_file(isolated, capsys):
    assert osctl_command.main(["--dry-run", "--env-file", "nope.env"]) == 0
    write_env(isolated / ".env", "DISCORD_BOT_TOKEN=made-up-file-token\n")
    assert osctl_command.main(["--dry-run"]) == 0
    body = osctl_command.serialize(osctl_command.build_commands()).decode()
    assert capsys.readouterr().out == (body + "\n") * 2  # both runs printed the commands


def test_with_no_env_file_the_shell_alone_still_works(sent, monkeypatch, capsys):
    monkeypatch.setenv("DISCORD_APPLICATION_ID", "7")
    assert osctl_command.main([]) == 0 and sent[0][1] == "/applications/7/commands"
    assert capsys.readouterr().err == ""  # no file, so nothing to announce


# ------------------------------------------------------------------ guilds (MALU-250)

def guild_pages(monkeypatch, pages, status=200):
    """Serve `pages` (lists of guild dicts, or raw text) in order; record each request."""
    monkeypatch.setenv("DISCORD_BOT_TOKEN", f"Bot {TOKEN}")
    recorded = []

    def fake_request(method, path, bot_token, body=None):
        recorded.append((method, path, bot_token))
        page = pages[len(recorded) - 1]
        return status, page if isinstance(page, str) else json.dumps(page)

    monkeypatch.setattr(osctl_command, "_request", fake_request)
    return recorded


def test_guilds_needs_only_the_token_and_reads_no_env_file(isolated, monkeypatch, capsys):
    # This file would be refused if read: it names the token.
    (isolated / ".env").write_text("DISCORD_BOT_TOKEN=x\n", encoding="utf-8")
    sent = guild_pages(monkeypatch, [[{"id": "11", "name": "Steppe Club"}, {"id": "12", "name": "Yurt"}]])
    assert osctl_command.main(["--guilds"]) == 0
    assert sent == [("GET", "/users/@me/guilds?limit=200&after=0", TOKEN)]
    out, err = capsys.readouterr()
    assert out == "11  Steppe Club\n12  Yurt\n" and err == ""


def test_guilds_follows_after_until_a_short_page(isolated, monkeypatch, capsys):
    full = [{"id": str(1000 + i), "name": f"s{i}"} for i in range(200)]
    sent = guild_pages(monkeypatch, [full, [{"id": "5000", "name": "last"}]])
    assert osctl_command.main(["--guilds"]) == 0
    assert [p for _, p, _ in sent] == ["/users/@me/guilds?limit=200&after=0",
                                       "/users/@me/guilds?limit=200&after=1199"]
    assert capsys.readouterr().out.splitlines()[-1] == "5000  last"


def test_guilds_stops_when_a_page_does_not_advance(isolated, monkeypatch, capsys):
    full = [{"id": "0", "name": "stuck"}] * 200
    sent = guild_pages(monkeypatch, [full, full])
    assert osctl_command.main(["--guilds"]) == 1
    assert len(sent) == 1 and "didn't advance" in capsys.readouterr().err


def test_guilds_says_none_for_no_servers_and_why_one_may_be_missing(isolated, monkeypatch, capsys):
    guild_pages(monkeypatch, [[]])
    assert osctl_command.main(["--guilds"]) == 0
    out, err = capsys.readouterr()
    assert out == "(none)\n"  # stdout stays the list alone; the hint goes to stderr
    assert "without the bot scope" in err and "approximate_guild_count" in err


def test_guilds_gives_no_hint_when_it_finds_servers(isolated, monkeypatch, capsys):
    guild_pages(monkeypatch, [[{"id": "11", "name": "Yurt"}]])
    assert osctl_command.main(["--guilds"]) == 0
    assert capsys.readouterr().err == ""


def test_guild_names_lose_control_and_format_characters(isolated, monkeypatch, capsys):
    name = "Evil\x1b[2J\nfake line\r‮esrever​"
    guild_pages(monkeypatch, [[{"id": "11", "name": name}]])
    assert osctl_command.main(["--guilds"]) == 0
    assert capsys.readouterr().out == "11  Evil[2Jfake lineesrever\n"


def test_guild_names_print_as_utf8_even_where_stdout_is_a_code_page(isolated, monkeypatch):
    """A pipe on Windows uses the locale's code page, which has no emoji: no UnicodeEncodeError mid-list."""
    import io
    import sys
    raw = io.BytesIO()
    monkeypatch.setattr(sys, "stdout", io.TextIOWrapper(raw, encoding="cp1252"))
    names = ["Café 🐎", "👨‍👩‍👧 family",
             "🏴󠁧󠁢󠁥󠁮󠁧󠁿 fans", None]
    guild_pages(monkeypatch, [[{"id": str(11 + i), "name": n} for i, n in enumerate(names)]])
    assert osctl_command.main(["--guilds"]) == 0
    sys.stdout.flush()
    assert raw.getvalue().decode("utf-8").splitlines() == [f"{11 + i}  {n or ''}" for i, n in enumerate(names)]


def test_guilds_without_a_token_fails_before_any_request(isolated, monkeypatch, capsys):
    sent = guild_pages(monkeypatch, [[]])
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    assert osctl_command.main(["--guilds"]) == 2
    assert sent == [] and "DISCORD_BOT_TOKEN" in capsys.readouterr().err


@pytest.mark.parametrize("status,body,expected", [
    (401, '{"message": "401: Unauthorized", "code": 0}', "Reset Token"),
    (403, '{"message": "Missing Access", "code": 50001}', "HTTP 403"),
    (200, '{"not": "a list"}', "unreadable"),
    (200, '[{"id": "abc", "name": "x"}]', "unreadable"),
])
def test_guilds_reports_a_failure_without_the_token(isolated, monkeypatch, capsys, status, body, expected):
    guild_pages(monkeypatch, [body], status=status)
    assert osctl_command.main(["--guilds"]) == 1
    out, err = capsys.readouterr()
    assert expected in out + err and TOKEN not in out + err
    assert "GUILD_ALLOWLIST" not in err and "per-server" not in err  # those hints are for register and list


# ------------------------------------------------------------------ get (MALU-250)

def test_get_sends_one_get_and_prints_the_json_cleaned(isolated, monkeypatch, capsys):
    (isolated / ".env").write_text("DISCORD_BOT_TOKEN=x\n", encoding="utf-8")  # refused if read
    answer = {"name": "Steppe \U0001f40e\x1b[2J‮", "bad\nkey": ["a\rb", 3, None, True]}
    sent = guild_pages(monkeypatch, [answer])
    assert osctl_command.main(["--get", "/applications/@me?with_counts=true"]) == 0
    assert sent == [("GET", "/applications/@me?with_counts=true", TOKEN)]
    out, err = capsys.readouterr()
    assert json.loads(out) == {"name": "Steppe \U0001f40e[2J", "badkey": ["ab", 3, None, True]}
    assert "\U0001f40e" in out and err == "" and TOKEN not in out


def test_get_prints_a_non_json_answer_cleaned(isolated, monkeypatch, capsys):
    guild_pages(monkeypatch, ["plain\x1b[31m text"])
    assert osctl_command.main(["--get", "/x"]) == 0
    assert capsys.readouterr().out == "plain[31m text\n"


@pytest.mark.parametrize("path", [
    "users/@me", "https://example.invalid/x", "//example.invalid/x", "/\\example.invalid", "/a/../b", "/..",
    "/a b", "/a\tb", "/a\nb", "/a#b", "", "/./a", "/%2e%2e/%2E%2e/x", "/a/%2e/b", "/guilds/é",
])
def test_get_refuses_a_path_that_isnt_a_plain_api_path(isolated, monkeypatch, capsys, path):
    sent = guild_pages(monkeypatch, [[]])
    assert osctl_command.main(["--get", path]) == 2
    assert sent == [] and "--get takes an API path" in capsys.readouterr().err


def test_get_without_a_path_or_token_sends_nothing(isolated, monkeypatch):
    sent = guild_pages(monkeypatch, [[]])
    assert osctl_command.main(["--get"]) == 2
    monkeypatch.delenv("DISCORD_BOT_TOKEN")
    assert osctl_command.main(["--get", "/users/@me"]) == 2
    assert sent == []


def test_get_reports_a_failure_without_the_token(isolated, monkeypatch, capsys):
    guild_pages(monkeypatch, ['{"message": "Missing Access", "code": 50001}'], status=403)
    assert osctl_command.main(["--get", "/guilds/1"]) == 1
    out, err = capsys.readouterr()
    assert "HTTP 403" in out and "Missing Access" in err and TOKEN not in out + err


def test_get_only_ever_uses_get(monkeypatch):
    """Read-only by construction: whatever the path, the method is GET and there is no body."""
    seen = []
    monkeypatch.setattr(osctl_command._OPENER, "open",
                        lambda request, timeout: seen.append(request) or (_ for _ in ()).throw(OSError("offline")))
    monkeypatch.setenv("DISCORD_BOT_TOKEN", TOKEN)
    with pytest.raises(OSError):
        osctl_command.main(["--get", "/applications/@me"])
    (request,) = seen
    assert request.get_method() == "GET" and request.data is None
    assert request.full_url == "https://discord.com/api/v10/applications/@me"


def test_get_redacts_credentials_in_the_answer(isolated, monkeypatch, capsys):
    hook = "https://discord.com/api/webhooks/123/made-up-webhook-token"
    answer = [{"id": "123", "token": "made-up-webhook-token", "url": hook, "client_secret": "made-up-secret",
               "access_token": "made-up-access", "avatar": None, "name": "hooks for /webhooks/9/x"}]
    guild_pages(monkeypatch, [answer])
    assert osctl_command.main(["--get", "/guilds/1/webhooks"]) == 0
    out = capsys.readouterr().out
    assert "made-up" not in out
    assert json.loads(out) == [{"id": "123", "token": "[redacted]",
                                "url": "https://discord.com/api/webhooks/123/[redacted]",
                                "client_secret": "[redacted]", "access_token": "[redacted]", "avatar": None,
                                "name": "hooks for /webhooks/9/[redacted]"}]


def test_get_redacts_and_cleans_an_error_or_non_json_answer(isolated, monkeypatch, capsys):
    guild_pages(monkeypatch, ["see /webhooks/5/made-up-token\x1b[2J"], status=400)
    assert osctl_command.main(["--get", "/x"]) == 1
    guild_pages(monkeypatch, ["see /webhooks/5/made-up-token\x1b[2J"])
    assert osctl_command.main(["--get", "/x"]) == 0
    out, err = capsys.readouterr()
    assert "made-up-token" not in out + err and "\x1b" not in out + err
    assert "/webhooks/5/[redacted]" in err and "/webhooks/5/[redacted]" in out


class _Redirecting(__import__("http.server").server.BaseHTTPRequestHandler):
    seen = []

    def do_GET(self):
        type(self).seen.append((self.path, self.headers.get("Authorization")))
        if self.path.startswith("/api/v10/"):
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{self.server.server_port}/elsewhere")
            self.end_headers()
        else:
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b"[]")

    def log_message(self, *args):
        pass


def test_a_redirect_is_a_failure_and_the_token_never_follows_it(isolated, monkeypatch, capsys):
    """urllib would copy Authorization onto the redirected request, to any host."""
    import http.server
    import threading
    server = http.server.HTTPServer(("127.0.0.1", 0), _Redirecting)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    _Redirecting.seen = []
    try:
        monkeypatch.setattr(osctl_command, "API_BASE", f"http://127.0.0.1:{server.server_port}/api/v10")
        monkeypatch.setenv("DISCORD_BOT_TOKEN", TOKEN)
        assert osctl_command.main(["--get", "/users/@me"]) == 1
    finally:
        server.shutdown()
        server.server_close()
    assert _Redirecting.seen == [("/api/v10/users/@me", f"Bot {TOKEN}")]  # nothing reached /elsewhere
    out, err = capsys.readouterr()
    assert "HTTP 302" in out and TOKEN not in out + err


def test_a_failed_get_redacts_a_webhook_token_in_the_echoed_path(isolated, monkeypatch, capsys):
    guild_pages(monkeypatch, ['{"message": "Unknown Webhook"}'], status=404)
    assert osctl_command.main(["--get", "/webhooks/1/made-up-webhook-token"]) == 1
    out, err = capsys.readouterr()
    assert "GET /webhooks/1/[redacted]: HTTP 404" in out and "made-up-webhook-token" not in out + err


def test_the_emoji_joiner_is_the_zero_width_joiner():
    assert "\u200d" in osctl_command._EMOJI_JOINERS
