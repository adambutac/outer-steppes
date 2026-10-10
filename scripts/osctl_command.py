"""Register the bridge's slash commands (DESIGN.md, Commands and Install contexts).

Run as `osctl command register|list|clear-guild|guilds|get`, which imports this module and
calls main(); `python scripts/osctl_command.py` still works. This is the
only code that uses the bot token's value. Run it from a separate environment,
never from the server's. It is not part of the server package, the server
never imports it, and the image leaves it out.

Commands are GLOBAL so the app can be both guild-installed and user-installed:
Discord applies `integration_types` and `contexts` only to global commands.

Environment:
  DISCORD_APPLICATION_ID  application id
  DISCORD_BOT_TOKEN       bot token (not needed with --dry-run). Shell only: never read from a file
  GUILD_ALLOWLIST         comma-separated guild ids; used by --list and --clear-guild when no
                          --guild is given
  OS_ENV_FILE             an env file to read the two settings above from (or pass --env-file);
                          without either, the repo root's .env if there is one. The shell wins.
                          A file that sets DISCORD_BOT_TOKEN is refused.

Usage:
  osctl command register                          register global commands (bulk overwrite)
  osctl command register --dry-run                print the JSON, no network
  osctl command list [--guild ID ...]             show what Discord has registered
  osctl command clear-guild [--guild ID ...]      delete every per-server command in these servers
  osctl command guilds                            list the servers the bot user is in
  osctl command get /PATH[?QUERY]                 GET any Discord API path; prints the JSON
  register, list and clear-guild take --env-file PATH. Run directly, this script takes the same
  flags, with --list for `command list`, --clear-guild for `command clear-guild`, --guilds for
  `command guilds` and --get PATH for `command get`.
"""

from __future__ import annotations

import http.client
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
# The only keys taken from an env file. The bot token is deliberately not one of them.
FILE_KEYS = ("DISCORD_APPLICATION_ID", "GUILD_ALLOWLIST")
TOKEN_KEY = "DISCORD_BOT_TOKEN"

API_BASE = "https://discord.com/api/v10"
USER_AGENT = "DiscordBot (outer-steppes osctl_command, 0.1.0)"

# Installable to servers (0) and to users (1); usable in servers (0), DMs with
# the bot (1), and group DMs / other DMs (2).
_EVERYWHERE = {"type": 1, "integration_types": [0, 1], "contexts": [0, 1, 2]}

ASK = {
    "name": "ask", **_EVERYWHERE,
    "description": "Ask your grok.com automation",
    "options": [
        {"name": "prompt", "description": "What to ask", "type": 3, "required": True,
         "min_length": 1, "max_length": 4000},
        {"name": "private", "description": "Only you see the answer (default: your /settings, private)",
         "type": 5, "required": False},
    ],
}
STATUS = {"name": "status", **_EVERYWHERE, "description": "Show whether /ask will work for you here"}
SETTINGS = {
    "name": "settings", **_EVERYWHERE,
    "description": "Show or change whether your answers are private by default",
    "options": [
        {"name": "private", "description": "true: only you see answers; false: everyone in the chat does",
         "type": 5, "required": False},
    ],
}
WEBHOOK = {
    "name": "webhook", **_EVERYWHERE,
    "description": "Your grok.com automation webhook",
    "options": [
        {"name": "set", "description": "Get a link to set or replace your webhook (never paste secrets in Discord)",
         "type": 1},
        {"name": "status", "description": "Whether your webhook and grok.com connector are set up", "type": 1},
        {"name": "remove", "description": "Remove your webhook; you stay a member", "type": 1},
    ],
}
LEAVE = {
    "name": "leave", **_EVERYWHERE,
    "description": "Leave Outer Steppes: removes your webhook and grok.com connection",
    "options": [
        {"name": "confirm", "description": "True to leave; without it, only explains what would be deleted",
         "type": 5, "required": False},
    ],
}


def build_commands() -> list[dict]:
    """The exact array to PUT. Role grants are not offered (ADR-0009)."""
    return [ASK, STATUS, SETTINGS, WEBHOOK, LEAVE]


def serialize(commands: list[dict]) -> bytes:
    return json.dumps(commands, separators=(",", ":"), ensure_ascii=False, indent=2).encode("utf-8")


def normalize_token(value: str) -> str:
    """Accept a token pasted with its "Bot " prefix; the header adds it again."""
    token = value.strip().strip('"').strip("'").strip()
    if token.lower().startswith("bot "):
        token = token[4:].strip()
    return token


HINTS = {
    401: "Discord rejected the bot token. Use Bot > Reset Token in the Developer Portal "
         "(not the public key or client secret), and paste the newest token only.",
    403: "The token works but cannot manage these commands. For per-server commands, check the "
         "guild id and that the bot is in that server.",
    404: "Unknown application or guild. Check DISCORD_APPLICATION_ID and GUILD_ALLOWLIST.",
}


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """urllib copies every header but the content ones onto a redirect, the bot token's
    Authorization included, to whatever host it names. Refused: a 3xx comes back as a failure."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


_OPENER = urllib.request.build_opener(_NoRedirect)


def _request(method: str, path: str, bot_token: str, body: bytes | None = None) -> tuple[int, str]:
    headers = {"Authorization": f"Bot {bot_token}", "User-Agent": USER_AGENT}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(f"{API_BASE}{path}", data=body, method=method, headers=headers)
    try:
        with _OPENER.open(request, timeout=30) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        # Discord's error body (message and code) holds no secrets.
        return exc.code, exc.read(500).decode("utf-8", "replace")


def _names(text: str) -> str:
    try:
        return ", ".join(c.get("name") for c in json.loads(text)) or "(none)"
    except (ValueError, AttributeError):
        return "(unreadable)"


def _report(label: str, status: int, text: str, hints: dict[int, str] = HINTS) -> bool:
    if 200 <= status < 300:
        print(f"{label}: HTTP {status}, Discord confirmed: {_names(text)}")
        return True
    print(f"{label}: HTTP {status}")
    if text:
        print(f"  discord says: {_redact_text(_printable(text))}", file=sys.stderr)
    if status in hints:
        print(f"  hint: {hints[status]}", file=sys.stderr)
    return False


class EnvFileError(Exception):
    """Explains what is wrong with the env file. The message never contains a value from it."""


def find_env_file(argv: list[str], environ) -> tuple[Path | None, bool]:
    """(path, discovered): --env-file, then OS_ENV_FILE, then the repo root's .env.

    An explicit path must exist. Only the repo root is searched, never the current
    directory or its parents: run from another project's folder, a cwd search would
    read that project's .env. No file at all is fine; the shell alone still works.
    """
    given = ""
    if "--env-file" in argv:
        i = argv.index("--env-file")
        given = argv[i + 1].strip() if i + 1 < len(argv) else ""
        if not given:
            raise EnvFileError("--env-file takes a path")
    given = given or environ.get("OS_ENV_FILE", "").strip()
    if given:
        path = Path(given).expanduser().resolve()
        if not path.is_file():
            raise EnvFileError(f"env file not found: {path}")
        return path, False
    candidate = REPO / ".env"
    return (candidate, True) if candidate.is_file() else (None, False)


def read_env_file(path: Path) -> dict[str, str]:
    """Only FILE_KEYS, last one wins. A file that names the bot token is refused.

    The repo root's .env is the server's env file, which must never hold the token
    (the server refuses to start with it), so a token here means the file is in the
    wrong place. Refused at any value, even empty, without printing it.
    """
    try:
        # utf-8-sig: Windows editors often add a BOM, which would hide a first-line key.
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise EnvFileError(f"{path} isn't UTF-8 (PowerShell 5's Out-File writes UTF-16); "
                           "save it as UTF-8") from None
    except OSError as exc:
        raise EnvFileError(f"can't read {path}: {exc.strerror}") from None
    found: dict[str, str] = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        key, sep, value = line.partition("=")
        key = key.strip()
        if key == TOKEN_KEY:
            raise EnvFileError(f"{path} sets {TOKEN_KEY}. The bot token comes only from the shell, never "
                               "from an env file: remove it from the file and set it in this shell")
        if key in FILE_KEYS and sep:  # a bare key has no value to take
            found[key] = value.strip()
    return found


def load_settings(argv: list[str], environ) -> dict[str, str]:
    """FILE_KEYS from the shell, and from the env file for each one the shell leaves empty."""
    path, discovered = find_env_file(argv, environ)
    from_file: dict[str, str] = {}
    if path is not None:
        if discovered:
            print(f"env file: {path}", file=sys.stderr)  # the path only, never the contents
        from_file = read_env_file(path)
    return {key: environ.get(key, "").strip() or from_file.get(key, "") for key in FILE_KEYS}


# Discord's maximum page size for GET /users/@me/guilds.
GUILDS_PAGE = 200


# Invisible format characters that emoji are built from: the zero-width joiner (family and
# profession emoji) and the tag characters (subdivision flags such as England's).
_EMOJI_JOINERS = {"\u200d", *map(chr, range(0xE0020, 0xE0080))}


def _printable(text: str) -> str:
    """A server name is other people's text: keep what str.isprintable() accepts, plus the
    characters emoji are joined with. That drops escape sequences, newlines and bidi overrides,
    which could rewrite the terminal or the line, and also non-ASCII spaces and characters
    Python's Unicode tables don't know."""
    return "".join(c for c in text if c.isprintable() or c in _EMOJI_JOINERS)


def _utf8_stdout() -> None:
    """A pipe or file on Windows defaults to the locale's code page, which can't hold an emoji
    name; print UTF-8 there too, as the console already does."""
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")


# An empty list is easy to misread: the app works in servers it was added to without the bot
# user, and Discord lists only those the bot user is a member of.
NO_GUILDS_HINT = ("  hint: the bot user is in no server. A server that added the app without the bot scope "
                  "isn't listed, though the app works there. `osctl command get /applications/@me` shows "
                  "approximate_guild_count, the servers that installed it; Server Settings > Integrations "
                  "shows it in a server, and Copy Server ID gives the id for `osctl command list --guild`.")


def list_guilds(bot_token: str) -> int:
    """Print the id and name of each server the bot user is in, following `after` page by page."""
    _utf8_stdout()
    after = 0
    count = 0
    while True:
        status, text = _request("GET", f"/users/@me/guilds?limit={GUILDS_PAGE}&after={after}", bot_token)
        if not 200 <= status < 300:
            # The other hints are about application ids and per-server commands, which this doesn't use.
            _report("guilds", status, text, hints={401: HINTS[401]})
            return 1
        try:
            page = [(int(g["id"]), str(g.get("name") or "")) for g in json.loads(text)]
        except (ValueError, TypeError, KeyError, AttributeError):
            print("guilds: Discord's answer was unreadable", file=sys.stderr)
            return 1
        for guild_id, name in page:
            print(f"{guild_id}  {_printable(name)}")
        count += len(page)
        if len(page) < GUILDS_PAGE:
            break
        last = max(guild_id for guild_id, _ in page)
        if last <= after:  # a page that doesn't move forward would loop for ever
            print("guilds: Discord's pages didn't advance; stopping", file=sys.stderr)
            return 1
        after = last
    if not count:
        print("(none)")
        print(NO_GUILDS_HINT, file=sys.stderr)
    return 0


REDACTED = "[redacted]"
# A webhook's URL holds its token: https://discord.com/api/webhooks/<id>/<token>.
_WEBHOOK_TOKEN = re.compile(r"(/webhooks/\d+/)[^/?#\s\"']+")


def _secret_key(key: str) -> bool:
    """Keys whose values are credentials: a webhook's `token`, and anything named like one."""
    key = key.lower()
    return key == "token" or key.endswith("_token") or "secret" in key


def _redact_text(text: str) -> str:
    return _WEBHOOK_TOKEN.sub(lambda m: m.group(1) + REDACTED, text)


def _clean(value):
    """Every string in a JSON value, keys included, through _printable; credentials redacted.
    Best effort: it knows the secrets Discord names, not every one an answer could hold."""
    if isinstance(value, str):
        return _redact_text(_printable(value))
    if isinstance(value, list):
        return [_clean(v) for v in value]
    if isinstance(value, dict):
        return {_printable(k): REDACTED if _secret_key(k) and v is not None else _clean(v)
                for k, v in value.items()}
    return value


def _api_path(path: str) -> str | None:
    """The path if it's a plain API path, else None. It's always appended to API_BASE, and
    can't start with // or hold a backslash, so the token goes only to Discord's API."""
    if not path.startswith("/") or path.startswith("//"):
        return None
    if not path.isascii():  # http.client can't send it; percent-encode it instead (%C3%A9)
        return None
    if "\\" in path or "#" in path or any(not c.isprintable() or c.isspace() for c in path):
        return None
    # Decoded first: %2e%2e is a .. segment to anything that normalizes the URL.
    if {".", ".."} & set(urllib.parse.unquote(path.split("?", 1)[0]).split("/")):
        return None
    return path


def get(bot_token: str, path: str) -> int:
    """GET one Discord API path with the bot token and print the answer, credentials redacted.
    Read-only by construction: the method is always GET, so this can't change the app or its
    commands."""
    _utf8_stdout()
    status, text = _request("GET", path, bot_token)
    if not 200 <= status < 300:
        _report(f"GET {_redact_text(path)}", status, text, hints={401: HINTS[401]})
        return 1
    try:
        print(json.dumps(_clean(json.loads(text)), ensure_ascii=False, indent=2))
    except ValueError:
        print(_redact_text(_printable(text)))
    return 0


def _guilds(argv: list[str], allowlist: str) -> list[str] | None:
    """Ids from repeated `--guild ID`, else GUILD_ALLOWLIST. None if an id is malformed."""
    given = [argv[i + 1] if i + 1 < len(argv) else "" for i, a in enumerate(argv) if a == "--guild"]
    ids = [g.strip() for g in given] if given else [g.strip() for g in allowlist.split(",") if g.strip()]
    # isascii too: str.isdigit() also accepts digits such as a superscript two, which aren't an id.
    return ids if all(g.isascii() and g.isdigit() for g in ids) else None


def clear_guilds(application_id: str, bot_token: str, guilds: list[str]) -> int:
    """Empty each server's per-server commands with a bulk overwrite of []. The app registers
    none, so any there are unwanted, such as a thief's (DESIGN.md, Stolen bot token). Only the
    per-server path is sent to; the global commands stay. Done only when Discord's answer is []."""
    ok = True
    for guild_id in guilds:
        label = f"guild {guild_id}"
        try:
            status, text = _request("PUT", f"/applications/{application_id}/guilds/{guild_id}/commands",
                                    bot_token, b"[]")
        # OSError covers URLError (DNS, refused) and timeouts; HTTPException a broken answer, such as
        # IncompleteRead or BadStatusLine. HTTPError is handled in _request.
        except (OSError, http.client.HTTPException) as exc:
            # One server's failure doesn't stop the rest. The reason never holds the token, which
            # travels only in a header.
            print(f"{label}: request failed: {type(exc).__name__}: {getattr(exc, 'reason', exc)}")
            ok = False
            continue
        if not 200 <= status < 300:
            _report(label, status, text)  # the failure line, Discord's error and a hint
            ok = False
            continue
        try:
            left = json.loads(text)
        except ValueError:
            left = None
        if left != []:
            # "cleared" is printed only for an answer of exactly []: anything else, {} included,
            # leaves it unconfirmed.
            print(f"{label}: HTTP {status}, but Discord didn't answer with an empty list; "
                  f"check with `osctl command list --guild {guild_id}`")
            ok = False
            continue
        print(f"{label}: per-server commands cleared (HTTP {status}, Discord confirmed: none left)")
    return 0 if ok else 1


def main(argv: list[str]) -> int:
    body = serialize(build_commands())
    if "--clear-guild" in argv and {"--dry-run", "--list", "--guilds", "--get"} & set(argv):
        # main picks a verb by which flags are present; osctl refuses these pairs too. A dry run or
        # a read must never turn into a clear, nor a clear into something else.
        print("--clear-guild runs on its own: no --dry-run (it has no dry run), --list, --guilds or --get",
              file=sys.stderr)
        return 2
    if "--dry-run" in argv:
        print(body.decode("utf-8"))
        return 0
    if "--guilds" in argv or "--get" in argv:
        # Need only the token: no application id, so no env file is read.
        path = None
        if "--get" in argv:
            i = argv.index("--get")
            path = _api_path(argv[i + 1]) if i + 1 < len(argv) else None
            if path is None:
                print("--get takes an API path starting with /, such as /users/@me/guilds?limit=10, "
                      "with no host, spaces, backslashes, # or .. segments, and non-ASCII "
                      "characters percent-encoded", file=sys.stderr)
                return 2
        bot_token = normalize_token(os.environ.get(TOKEN_KEY, ""))
        if not bot_token:
            print("DISCORD_BOT_TOKEN is required, from this shell", file=sys.stderr)
            return 2
        return get(bot_token, path) if path is not None else list_guilds(bot_token)

    try:
        settings = load_settings(argv, os.environ)
    except EnvFileError as exc:
        print(exc, file=sys.stderr)
        return 2
    application_id = settings["DISCORD_APPLICATION_ID"]
    bot_token = normalize_token(os.environ.get(TOKEN_KEY, ""))  # the shell only, never a file
    if not application_id or not bot_token:
        print("DISCORD_APPLICATION_ID and DISCORD_BOT_TOKEN are required (the token from this shell, "
              "the application id from this shell or an env file)", file=sys.stderr)
        return 2
    if not (application_id.isascii() and application_id.isdigit()):
        # It's part of every path below: "1/commands?x=" would turn clear-guild's per-server PUT
        # of [] into one that empties the global commands.
        print("DISCORD_APPLICATION_ID must be a numeric application id", file=sys.stderr)
        return 2

    guilds: list[str] = []
    if "--list" in argv or "--clear-guild" in argv:  # register never touches per-server commands
        guilds = _guilds(argv, settings["GUILD_ALLOWLIST"])
        if guilds is None:
            print("--guild takes a numeric server id", file=sys.stderr)
            return 2

    if "--clear-guild" in argv:
        if not guilds:
            print("clear-guild needs server ids: pass --guild ID (from `osctl command guilds`), "
                  "or set GUILD_ALLOWLIST in this shell or an env file", file=sys.stderr)
            return 2
        return clear_guilds(application_id, bot_token, guilds)

    if "--list" in argv:
        status, text = _request("GET", f"/applications/{application_id}/commands", bot_token)
        print(f"global: {_names(text) if status == 200 else f'HTTP {status}: {text}'}")
        for guild_id in guilds:
            status, text = _request("GET", f"/applications/{application_id}/guilds/{guild_id}/commands", bot_token)
            print(f"guild {guild_id}: {_names(text) if status == 200 else f'HTTP {status}: {text}'}")
        return 0

    ok = _report(f"application {application_id}, global",
                 *_request("PUT", f"/applications/{application_id}/commands", bot_token, body))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
