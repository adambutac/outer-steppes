"""The one entry point for the operator and developer tools (DESIGN.md, Registration (operator)).

Commands are grouped by what they act on, noun then verb:

  container ...               the local Podman container and Funnel: scripts/osctl_container.py, host only
  member list                 active members: Discord id, membership id, label, joined, webhook, connector
  member add USER_ID [--label TEXT] [--from-file PATH]   add the member if needed, and set their webhook
  member remove USER_ID       end the membership: webhook, connector connections, everything
  webhook set USER_ID [--from-file PATH]   replace an existing member's webhook; never adds a member
  webhook remove USER_ID      remove only the member's webhook; they stay a member, connector included
  connector settings          the shared values every member enters in grok.com (Discord login)
  connector list USER_ID      a member's grok.com connector connections
  connector disconnect USER_ID [CONNECTION_ID]   disconnect one connection, or all
  connector client add|list USER_ID, client delete CLIENT_ID   per-member OAuth clients
  bridge pause|unpause|status
  bridge claim [--if-unclaimed]   a one-time link the owner opens to become the first member (1 hour)
  command register|list|clear-guild|guilds|get ...   the slash commands, the bot's servers, any API GET: scripts/osctl_command.py, host only, bot token

The API groups (member, webhook, connector, bridge) call the admin listener
directly where OSCTL_ADMIN_URL is set, as inside the container. Where it is
unset or empty, they run the same command inside the outer-steppes container
with `podman exec`, through scripts/osctl_container.py. `command` always runs
where it is typed, because the bot token is in that shell and must never enter
the container. This file is the only one the image ships; it imports
osctl_container.py or osctl_command.py only when the chosen group needs it,
and never both.

The automation URL and signing secret are read from a hidden prompt or from
a 0600 file, never from command-line arguments (which show up in process
lists). Forwarded into the container without a terminal, the prompt reads
piped stdin instead, because getpass there finds no tty.

Environment:
  OSCTL_ADMIN_URL   admin listener base URL; unset or empty: the container
  BRIDGE_REGISTRATION_TOKEN or BRIDGE_REGISTRATION_TOKEN_FILE   the admin bearer, read only
                    when calling the admin listener directly
"""

from __future__ import annotations

import argparse
import getpass
import importlib.util
import json
import os
import stat
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

PAUSE_WAIT_SECONDS = 30
# osctl_container.py and osctl_command.py sit beside this file in the
# repository. The image copies only this file, so inside the container neither
# is found.
SCRIPTS = Path(__file__).resolve().parent


class OsctlError(Exception):
    pass


def load_token(env=os.environ) -> str:
    inline, path = env.get("BRIDGE_REGISTRATION_TOKEN"), env.get("BRIDGE_REGISTRATION_TOKEN_FILE")
    if inline and path:
        raise OsctlError("set only one of BRIDGE_REGISTRATION_TOKEN and BRIDGE_REGISTRATION_TOKEN_FILE")
    if path:
        with open(path, encoding="utf-8") as handle:
            return handle.read().strip()
    if inline:
        return inline.strip()
    raise OsctlError("BRIDGE_REGISTRATION_TOKEN or BRIDGE_REGISTRATION_TOKEN_FILE is required")


def read_secrets_from_file(path: str) -> tuple[str, str]:
    """Line 1: automation URL. Line 2: signing secret. The file must be 0600 on POSIX."""
    if os.name == "posix" and stat.S_IMODE(os.stat(path).st_mode) & 0o077:
        raise OsctlError(f"{path} must not be readable by group or others (chmod 600)")
    with open(path, encoding="utf-8") as handle:
        lines = [line.strip() for line in handle if line.strip()]
    if len(lines) != 2:
        raise OsctlError(f"{path} must contain exactly two lines: the automation URL, then the signing secret")
    return lines[0], lines[1]


def read_secrets_interactively(prompt=getpass.getpass) -> tuple[str, str]:
    url = prompt("Automation URL (input hidden): ").strip()
    secret = prompt("Secret or key, whsec_... or crsr_... (input hidden): ").strip()
    return url, secret


class AdminClient:
    def __init__(self, base_url: str, token: str) -> None:
        self._base = base_url.rstrip("/")
        self._token = token

    def request(self, method: str, path: str, body: dict | None = None) -> tuple[int, object]:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self._base + path, data=data, method=method, headers={
            "Authorization": f"Bearer {self._token}",
            "Content-Type": "application/json",
        })
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                return resp.status, _parse(resp.read())
        except urllib.error.HTTPError as exc:
            return exc.code, _parse(exc.read())
        except urllib.error.URLError as exc:
            raise OsctlError(f"cannot reach the admin listener at {self._base}: {exc.reason}") from None


def _parse(raw: bytes) -> object:
    try:
        return json.loads(raw) if raw else None
    except ValueError:
        return None


EXPLAIN = {
    401: "the admin listener rejected the bearer token (check BRIDGE_REGISTRATION_TOKEN)",
    403: "the bridge is paused; run `osctl bridge unpause` first",
}


NO_REGISTRATION = "no active membership for that Discord user id"
CLAIM_REFUSED = {
    "claimed": "the bridge already has a member, so it's claimed; `osctl member list` shows who",
    "operator_mode": "MEMBERSHIP=operator: there's no claim link; add members with `osctl member add`",
}
# The refusals that mean no link is needed, which `bridge claim --if-unclaimed` keeps quiet about.
NOTHING_TO_CLAIM = {(409, "claimed"), (409, "operator_mode"), (404, "discord_login_off")}


# What to do about the save refusals of ADR-0018 (DESIGN.md, Another member's connector).
SAVE_REFUSALS = {
    "secret_banned": "secret_banned: that signing secret is banned; rotate it in grok.com, or create a new "
                     "automation, and set that",
    "url_in_use": "url_in_use: another member's automation holds that trigger URL; create a new automation in "
                  "grok.com and set its URL and secret",
}


def _check(status: int, payload: object, on_404: str | None = None) -> object:
    if 200 <= status < 300:
        return payload
    detail = payload.get("error") if isinstance(payload, dict) else None
    if status == 404 and on_404 and detail == "not_found":
        detail = on_404
    detail = SAVE_REFUSALS.get(detail, detail)
    raise OsctlError(f"HTTP {status}: {detail or EXPLAIN.get(status, 'request failed')}")


USER_ID_HELP = "the member's numeric Discord user id (Developer Mode -> right-click the member -> Copy User ID)"


def discord_user_id(value: str) -> str:
    """Refuse a username before it reaches the API, where it could only be a confusing 404."""
    value = value.strip()
    if not value.isascii() or not value.isdigit() or len(value) > 20:
        raise argparse.ArgumentTypeError(
            f"'{value}' is not a Discord user id. It must be digits (Developer Mode -> right-click the member -> "
            "Copy User ID), not a username")
    return value


def _user_id_arg(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("user_id", metavar="DISCORD_USER_ID", type=discord_user_id, help=USER_ID_HELP)
    return parser


def _from_file_arg(parser: argparse.ArgumentParser) -> argparse.ArgumentParser:
    parser.add_argument("--from-file", metavar="PATH",
                        help="0600 file: line 1 automation URL, line 2 signing secret. "
                             "Only where OSCTL_ADMIN_URL is set; forwarded, pipe them to stdin instead")
    return parser


def _verbs(sub, name: str, help: str):
    return sub.add_parser(name, help=help).add_subparsers(dest="verb", required=True, metavar="VERB")


def build_parser() -> argparse.ArgumentParser:
    """Every leaf sets `action`, "group verb", which run() dispatches on."""
    parser = argparse.ArgumentParser(prog="osctl", description="Outer Steppes operator and developer CLI")
    groups = parser.add_subparsers(dest="group", required=True, metavar="GROUP")
    # Listed for --help only: main() hands everything after `container` to osctl_container.py untouched.
    groups.add_parser("container", help="the local Podman container and Funnel (host only; "
                                        "`osctl container --help`)")

    member = _verbs(groups, "member", "members of the bridge")
    member.add_parser("list", help="list active members (never shows webhook URLs or secrets)") \
        .set_defaults(action="member list")
    add = _from_file_arg(_user_id_arg(member.add_parser("add", help="add a member if needed, and set their webhook")))
    add.add_argument("--label", help="a note for the operator, e.g. the member's name")
    add.set_defaults(action="member add")
    _user_id_arg(member.add_parser("remove", help="end a membership: webhook, connector connections, everything")) \
        .set_defaults(action="member remove")

    webhook = _verbs(groups, "webhook", "a member's grok.com webhook")
    _from_file_arg(_user_id_arg(webhook.add_parser("set", help="replace an existing member's webhook; "
                                                              "never adds a member"))) \
        .set_defaults(action="webhook set")
    _user_id_arg(webhook.add_parser("remove", help="remove only a member's webhook; the member and their "
                                                   "connector stay")) \
        .set_defaults(action="webhook remove")

    connector = _verbs(groups, "connector", "the grok.com connector")
    connector.add_parser("settings", help="print the shared grok.com connector values (Discord login)") \
        .set_defaults(action="connector settings")
    _user_id_arg(connector.add_parser("list", help="list a member's connector connections")) \
        .set_defaults(action="connector list")
    disconnect = _user_id_arg(connector.add_parser("disconnect",
                                                   help="disconnect one connector connection, or all of them"))
    disconnect.add_argument("connection_id", nargs="?", help="from `osctl connector list`; omit for all")
    disconnect.set_defaults(action="connector disconnect")
    client = connector.add_parser("client", help="per-member OAuth clients") \
        .add_subparsers(dest="client_verb", required=True, metavar="VERB")
    _user_id_arg(client.add_parser("add", help="issue an OAuth client for grok.com's custom-connector form")) \
        .set_defaults(action="connector client add")
    _user_id_arg(client.add_parser("list", help="list a member's OAuth clients")) \
        .set_defaults(action="connector client list")
    delete = client.add_parser("delete", help="revoke one OAuth client and its tokens")
    delete.add_argument("client_id")
    delete.set_defaults(action="connector client delete")

    bridge = _verbs(groups, "bridge", "the kill switch and the claim link")
    bridge.add_parser("pause", help="stop new work and withdraw pending requests").set_defaults(action="bridge pause")
    bridge.add_parser("unpause", help="resume").set_defaults(action="bridge unpause")
    bridge.add_parser("status", help="show pause state and pending final messages") \
        .set_defaults(action="bridge status")
    claim = bridge.add_parser("claim", help="a one-time link the owner opens to become the first member")
    claim.add_argument("--if-unclaimed", action="store_true",
                       help="print nothing and succeed when there's nothing to claim: already claimed, "
                            "MEMBERSHIP=operator, or Discord login off")
    claim.set_defaults(action="bridge claim")

    command = _verbs(groups, "command", "Discord slash commands (host only; DISCORD_BOT_TOKEN from this shell)")
    register = command.add_parser("register", help="register the global commands (bulk overwrite)")
    register.add_argument("--dry-run", action="store_true", help="print the JSON; no token, no network")
    register.set_defaults(action="command register")
    listing = command.add_parser("list", help="show what Discord has registered, globally and per --guild server")
    listing.set_defaults(action="command list")
    listing.add_argument("--guild", action="append", metavar="ID",
                         help="a server id; repeatable (default: GUILD_ALLOWLIST)")
    clearing = command.add_parser("clear-guild", help="delete every per-server command in each --guild server; "
                                                      "the global commands stay")
    clearing.set_defaults(action="command clear-guild")
    clearing.add_argument("--guild", action="append", metavar="ID",
                          help="a server id; repeatable (default: GUILD_ALLOWLIST)")
    command.add_parser("guilds", help="list the servers the bot user is in, by id and name") \
        .set_defaults(action="command guilds")
    getter = command.add_parser("get", help="GET any Discord API path with the bot token and print the JSON")
    getter.add_argument("path", help="starting with /, e.g. /users/@me or /applications/@me")
    getter.set_defaults(action="command get")
    for p in (register, listing, clearing):
        p.add_argument("--env-file", metavar="PATH",
                       help="for DISCORD_APPLICATION_ID and GUILD_ALLOWLIST; defaults to OS_ENV_FILE, then .env")
    return parser


def run(argv: list[str], client: AdminClient, out=sys.stdout, prompt=getpass.getpass,
        sleep=time.sleep) -> int:
    """An API group's command, against the admin listener."""
    args = build_parser().parse_args(argv)
    action = args.action

    if action in ("member add", "webhook set"):
        if action == "webhook set":
            # The POST below also adds a non-member, which `webhook set` must never do.
            members = _check(*client.request("GET", "/v1/registrations")) or []
            if not any(m["discord_user_id"] == args.user_id for m in members):
                raise OsctlError(f"{NO_REGISTRATION}; `osctl member add` adds a member")
        url, secret = (read_secrets_from_file(args.from_file) if args.from_file
                       else read_secrets_interactively(prompt))
        body = {"discord_user_id": args.user_id, "automation_url": url, "signing_secret": secret}
        if getattr(args, "label", None) is not None:
            body["label"] = args.label
        result = _check(*client.request("POST", "/v1/registrations", body))
        verb = "replaced the webhook" if result["replaced"] else "set the webhook"
        print(f"{verb} for member {result['discord_user_id']} (registration {result['id']}); "
              f"{result['withdrawn']} pending request(s) withdrawn", file=out)
    elif action == "member list":
        members = _check(*client.request("GET", "/v1/registrations")) or []
        for m in members:
            # The membership id is what log lines carry (DESIGN.md, Another member's connector).
            webhook = m["automation"] or "no webhook"
            if m["secret_banned"]:
                webhook += " (secret banned)"
            used = "never used" if m["last_used_at"] is None else f"last used {m['last_used_at']}"
            connector = f"{m['connections']} connection(s), {used}"
            print(f"{m['discord_user_id']}  membership {m['id']}  {m['label'] or '-'}  joined {m['created_at']}  "
                  f"{webhook}  {connector}", file=out)
        print(f"{len(members)} member(s)", file=out)
    elif action == "member remove":
        result = _check(*client.request("DELETE", f"/v1/registrations/{args.user_id}"), on_404=NO_REGISTRATION)
        print(f"removed the member; {result['withdrawn']} pending request(s) withdrawn", file=out)
    elif action == "webhook remove":
        result = _check(*client.request("DELETE", f"/v1/registrations/{args.user_id}/automation"),
                        on_404=NO_REGISTRATION)
        if result["removed"]:
            print(f"removed the webhook; {result['withdrawn']} pending request(s) withdrawn. The member and their "
                  "grok.com connector stay", file=out)
        else:
            print("that member has no webhook set; nothing to remove", file=out)
    elif action == "connector client add":
        form = _check(*client.request("POST", f"/v1/registrations/{args.user_id}/mcp-clients"),
                      on_404=NO_REGISTRATION)
        print("Add a custom connector at grok.com/connectors (New Connector -> Custom) with:", file=out)
        for label, key in (("MCP server URL", "mcp_server_url"), ("Client ID", "client_id"),
                           ("Client secret", "client_secret"), ("Authorization endpoint", "authorization_endpoint"),
                           ("Token endpoint", "token_endpoint"), ("Scopes", "scopes"),
                           ("Token auth method", "token_auth_method")):
            print(f"  {label + ':':24}{form[key]}", file=out)
        print("  Connector name:         Outer Steppes  (suggested; Grok then names the tool", file=out)
        print("                          outer_steppes___discord_reply, which the automation prompt uses)", file=out)
        print("The client secret is shown only now. Hand it to the owner privately.", file=out)
    elif action == "connector client list":
        for item in _check(*client.request("GET", f"/v1/registrations/{args.user_id}/mcp-clients"),
                           on_404=NO_REGISTRATION) or []:
            print(f"{item['client_id']}  created {item['created_at']}  last used {item['last_used_at']}", file=out)
    elif action == "connector client delete":
        _check(*client.request("DELETE", f"/v1/mcp-clients/{args.client_id}"))
        print("deleted; its tokens no longer work", file=out)
    elif action == "connector list":
        for item in _check(*client.request("GET", f"/v1/registrations/{args.user_id}/mcp-connections"),
                           on_404=NO_REGISTRATION) or []:
            print(f"{item['connection_id']}  {item['client_kind']}  created {item['created_at']}  "
                  f"last used {item['last_used_at']}", file=out)
    elif action == "connector disconnect":
        path = f"/v1/registrations/{args.user_id}/mcp-connections"
        if args.connection_id:
            path += f"/{args.connection_id}"
        result = _check(*client.request("DELETE", path),
                        on_404=NO_REGISTRATION + (", or no such connection" if args.connection_id else ""))
        # Observed in the PR 8a pilot: an automation run shows no re-authentication
        # card; the member must reconnect from grok.com's connector settings.
        print(f"disconnected {result['disconnected']} connection(s); the member must reconnect Outer Steppes "
              "in grok.com's connector settings", file=out)
    elif action == "connector settings":
        status, payload = client.request("GET", "/v1/connector-settings")
        if status == 404:
            raise OsctlError("Discord login is off: set DISCORD_CLIENT_SECRET in the bridge's environment and "
                             "restart it (in a container, `osctl container restart`: --env-file is read only "
                             "at creation)")
        form = _check(status, payload)
        print("Every member adds a custom connector at grok.com/connectors (New Connector -> Custom) with:", file=out)
        for label, key in (("Connector name", "connector_name"), ("MCP server URL", "mcp_server_url"),
                           ("Client ID", "client_id"), ("Authorization endpoint", "authorization_endpoint"),
                           ("Token endpoint", "token_endpoint"), ("Scopes", "scopes"),
                           ("Token auth method", "token_auth_method")):
            print(f"  {label + ':':24}{form[key]}", file=out)
        print(f"  {'Client secret:':24}{form['client_secret'] or '(leave empty)'}", file=out)
        print("None of these are secrets: members sign in with Discord when they connect.", file=out)
        print(f"Discord Developer Portal > OAuth2 > Redirects must list: {form['discord_redirect_url']}", file=out)
    elif action == "bridge pause":
        result = _check(*client.request("POST", "/v1/pause"))
        print(f"paused; {result['withdrawn']} pending request(s) withdrawn", file=out)
        return _wait_for_withdrawals(client, out, sleep)
    elif action == "bridge unpause":
        _check(*client.request("DELETE", "/v1/pause"))
        print("unpaused", file=out)
    elif action == "bridge status":
        result = _check(*client.request("GET", "/v1/status"))
        print(f"paused: {result['paused']}; pending final messages: {result['pending_final_messages']}", file=out)
    elif action == "bridge claim":
        status, payload = client.request("POST", "/v1/claim")
        detail = payload.get("error") if isinstance(payload, dict) else None
        # `osctl container up` asks on every start, claimed or not. Any other failure (paused,
        # the bearer, an unreachable listener) still says why and exits nonzero.
        if args.if_unclaimed and (status, detail) in NOTHING_TO_CLAIM:
            return 0
        if status == 409 and detail in CLAIM_REFUSED:
            raise OsctlError(CLAIM_REFUSED[detail])
        if status == 404 and detail == "discord_login_off":
            raise OsctlError("Discord login is off, so the bridge can't be claimed; add members with "
                             "`osctl member add`")
        result = _check(status, payload)
        expires = time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(result["expires_at"]))
        print("Open this link in your own browser and sign in with Discord to become the bridge's first member:",
              file=out)
        print(f"  {result['claim_url']}", file=out)
        print(f"It works once, until {expires}, and replaces any earlier link. Whoever opens it first "
              "becomes the owner, so don't paste it anywhere else.", file=out)
    else:
        raise OsctlError(f"`osctl {action}` doesn't call the admin API")
    return 0


def _wait_for_withdrawals(client: AdminClient, out, sleep) -> int:
    """DESIGN.md: wait up to 30 s for the finalizer and report what is still pending."""
    pending = 0
    for _ in range(PAUSE_WAIT_SECONDS):
        pending = _check(*client.request("GET", "/v1/status"))["pending_final_messages"]
        if pending == 0:
            print("all withdrawal messages sent; safe to stop the process", file=out)
            return 0
        sleep(1)
    print(f"{pending} withdrawal message(s) still pending; they are retried at the next startup "
          "while their interaction tokens are usable", file=out)
    return 1


def load_script(name: str):
    """scripts/<name>.py as a module, or None where it isn't beside this file (inside the image)."""
    path = SCRIPTS / f"{name}.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _host_tool(name: str, group: str, load):
    module = load(name)
    if module is None:
        raise OsctlError(f"`osctl {group}` runs on the host, from the repository: scripts/{name}.py isn't here")
    return module


def command_argv(args: argparse.Namespace) -> list[str]:
    """osctl_command.py's own flags for `osctl command register|list|clear-guild|guilds|get`."""
    argv = {"command list": ["--list"], "command clear-guild": ["--clear-guild"],
            "command guilds": ["--guilds"]}.get(args.action, [])
    if args.action == "command get":
        argv = ["--get", args.path]
    if getattr(args, "dry_run", False):
        argv.append("--dry-run")
    for guild in getattr(args, "guild", None) or []:
        argv += ["--guild", guild]
    if getattr(args, "env_file", None):
        argv += ["--env-file", args.env_file]
    return argv


def main(argv: list[str], env=None, load=load_script) -> int:
    env = os.environ if env is None else env
    try:
        if argv[:1] == ["container"]:
            # Handed over before argparse, which would claim options like --help or --env-file.
            return _host_tool("osctl_container", "container", load).main(argv[1:], prog="osctl container")
        args = build_parser().parse_args(argv)
        if args.group == "command":
            # Always where it's typed, never through podman exec: the bot token is in this shell.
            return _host_tool("osctl_command", "command", load).main(command_argv(args))
        admin_url = env.get("OSCTL_ADMIN_URL", "").strip()
        if admin_url:
            # The registration bearer is read only here, on the direct path.
            return run(argv, AdminClient(admin_url, load_token(env)))
        if getattr(args, "from_file", None):
            # The path would be opened inside the container, where it doesn't exist or is another file.
            raise OsctlError("--from-file can't be forwarded into the container: leave it out to be prompted, "
                             "or pipe the two lines (automation URL, then signing secret) to stdin")
        dev = load("osctl_container")
        if dev is None:
            raise OsctlError("OSCTL_ADMIN_URL is unset or empty: set it to the admin listener's URL "
                             "(the image sets http://127.0.0.1:8081)")
        try:
            return dev.Dev(env=env).osctl(argv)
        except dev.DevError as exc:  # podman isn't on PATH
            raise OsctlError(str(exc)) from None
    except (OsctlError, OSError) as exc:
        print(f"osctl: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
