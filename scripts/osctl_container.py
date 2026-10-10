"""Local development wrapper: the bridge in Podman on this machine, behind
Tailscale Funnel (DESIGN.md, Deployment and rollout, Local development).
Run as `osctl container ...`; `python scripts/osctl_container.py ...` still works.

Saves retyping the `podman run` flags. It only shells out to `podman` and
`tailscale` with argument lists, and keeps the manual commands' rules: publish on
127.0.0.1 only, never publish or funnel the admin port, pass the env file by path
and never print it.

Commands:
  build                     build the image, tagged outer-steppes and outer-steppes:<commit>
  up [--build]              start the container (a stopped one is recreated); on an unclaimed
                            bridge, print a claim link
  down                      stop and remove the container; the data volume stays
  restart [--build]         down, then up: picks up a new image or env file
  status                    container state and health, then `osctl bridge status`
  logs [-f] [--tail N]      the container's logs
  exec [CMD...]             any command inside the container (default: sh)
  tunnel [--url URL]        Funnel to 127.0.0.1:8080 until Ctrl-C, at this machine's ts.net URL
  db-snapshot [--out DIR]   a consistent copy of the database, written outside the repository
  reset [--build] [--no-snapshot] [--yes]
                            delete the data volume and start on an empty database; asks first,
                            and exports the old volume outside the repository unless --no-snapshot
  restore FILE [--no-snapshot] [--yes]
                            replace the data volume with a snapshot (a .tar from reset or a .db
                            from db-snapshot) and start on it; asks first, checks the snapshot
                            against the image's schema before anything stops, and exports the
                            current volume unless --no-snapshot

Environment:
  OS_ENV_FILE       the bridge's env file, as for `podman run --env-file` (or pass --env-file);
                    without either, the repo root's .env
  OS_VOLUME         the data volume (default outer-steppes-data)
  OS_SNAPSHOT_DIR   where db-snapshot, reset and restore write (default ~/outer-steppes-snapshots)
"""

from __future__ import annotations

import argparse
import codecs
import contextlib
import datetime
import json
import os
import posixpath
import shutil
import sqlite3
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
IMAGE = "outer-steppes"
CONTAINER = "outer-steppes"
DEFAULT_VOLUME = "outer-steppes-data"
DATA_DIR = "/var/lib/outer-steppes"
# The database's file name in DATA_DIR, and the user the image runs as (deploy/Dockerfile;
# a test keeps them in step).
DB_NAME = "bridge.db"
IMAGE_USER = "outer-steppes"
# Loopback only: a bare 8080:8080 would expose the bridge on every interface,
# and whether a host firewall still filters a published port depends on the
# runtime and its mode (DESIGN.md, Layouts).
PUBLISH = "127.0.0.1:8080:8080"
UPSTREAM = "127.0.0.1:8080"
HEALTHZ = f"http://{UPSTREAM}/healthz"
DEFAULT_SNAPSHOT_DIR = Path.home() / "outer-steppes-snapshots"
# Every variable deploy/Dockerfile sets for the container layout. Overriding one
# breaks the container (binds, database) or osctl inside it (OSCTL_ADMIN_URL).
IMAGE_OWNED_KEYS = {"PUBLIC_BIND", "ADMIN_BIND", "DATABASE_PATH", "OSCTL_ADMIN_URL"}
# Keys the server refuses to start with (config.FORBIDDEN_VARS; a test keeps them
# in step). Refused here before podman run, because by the
# time the bridge refuses, the value already sits in the container's saved config:
# a bot token would be readable with `podman inspect` until `down`.
REFUSED_KEYS = {"DISCORD_BOT_TOKEN", "LOG_PROMPTS"}

# Runs inside the container, where DATABASE_PATH comes from the container's own
# env. SQLite's online backup gives a consistent copy while the bridge writes.
# mode=rw so a wrong path fails instead of creating an empty database.
SNAPSHOT_SCRIPT = (
    "import os, sqlite3, sys\n"
    "os.umask(0o077)\n"
    "src = sqlite3.connect('file:' + os.environ['DATABASE_PATH'] + '?mode=rw', uri=True)\n"
    "dst = sqlite3.connect(sys.argv[1])\n"
    "src.backup(dst)\n"
    "dst.close()\n"
    "src.close()\n"
)

# Runs in a throwaway container of the image, so a snapshot is checked against the schema
# constants of the image that will run it. It repeats the version and tables check startup
# makes (db.initialize) instead of calling it, because initialize would create a schema on an
# empty file and only says the tables differ, where this names them. Keep it in step with
# initialize; once migrations exist, it must accept what initialize migrates.
# Like startup, it doesn't see a changed column or constraint: such a snapshot is restored and
# fails at the first write that uses it.
# Exits 3 with a one-line reason on a mismatch. Nothing is migrated (no users yet).
SCHEMA_CHECK_SCRIPT = (
    "import os, sqlite3, sys\n"
    "from outer_steppes import db\n"
    "path = sys.argv[1]\n"
    "if not os.path.isfile(path):\n"
    "    print('it has no ' + os.path.basename(path))\n"
    "    sys.exit(3)\n"
    "try:\n"
    "    conn = sqlite3.connect('file:' + path + '?mode=rw', uri=True)\n"
    "    version = conn.execute('PRAGMA user_version').fetchone()[0]\n"
    "    tables = {r[0] for r in conn.execute(\"SELECT name FROM sqlite_master \"\n"
    "              \"WHERE type = 'table' AND name NOT LIKE 'sqlite_%'\")}\n"
    "except sqlite3.DatabaseError as exc:\n"
    "    print('it is not a readable SQLite database: ' + str(exc))\n"
    "    sys.exit(3)\n"
    "problems = []\n"
    "if version != db.SCHEMA_VERSION:\n"
    "    problems.append(f'schema version {version}, expected {db.SCHEMA_VERSION}')\n"
    "extra, missing = sorted(tables - db.BASE_TABLES), sorted(db.BASE_TABLES - tables)\n"
    "if extra:\n"
    "    problems.append('extra tables: ' + ', '.join(extra))\n"
    "if missing:\n"
    "    problems.append('missing tables: ' + ', '.join(missing))\n"
    "if problems:\n"
    "    print('; '.join(problems))\n"
    "    sys.exit(3)\n"
)
# `podman volume import` leaves the volume's root owned by root with mode 755, and the bridge
# refuses a database directory others can read (db.check_file_modes). Run as root in a
# throwaway container of the image, so the image's own user name resolves.
SCRATCH_LABEL = "outer-steppes.restore-check"
FIX_OWNER_SCRIPT = f"chown -R {IMAGE_USER}:{IMAGE_USER} {DATA_DIR} && chmod -R go-rwx {DATA_DIR}"


class DevError(Exception):
    pass


def default_runner(cmd: list[str], capture: bool = False) -> tuple[int, str]:
    try:
        # UTF-8, not the locale codec: on Windows that is cp1252, and `tailscale status
        # --json` lists every peer's name, which can hold bytes cp1252 can't decode.
        done = subprocess.run(cmd, cwd=REPO, text=True, encoding="utf-8", errors="replace",
                              capture_output=capture)
    except FileNotFoundError:
        raise DevError(f"{cmd[0]} not found on PATH") from None
    return done.returncode, (done.stdout or "") if capture else ""


@contextlib.contextmanager
def private_files():
    """A 077 umask while a child process writes a copy of the database, so the file is never
    readable by others, even while it's written into an existing, looser directory (POSIX only)."""
    if os.name == "nt":
        yield
        return
    old = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(old)


def is_interactive() -> bool:
    # stdin only: with stdout piped (`osctl member add ... | tee`), no -t would make getpass
    # inside the container fall back to reading stdin with echo, so the URL and whsec_ would
    # show on screen as typed.
    return sys.stdin.isatty()


def inside_repo(path: Path) -> bool:
    path = path.resolve()
    return path == REPO or REPO in path.parents


class Dev:
    def __init__(self, runner=default_runner, env=None, out=sys.stdout, err=sys.stderr,
                 interactive=is_interactive, wait_healthy=None, ask=input):
        self.runner = runner
        self.env = os.environ if env is None else env
        self.out = out
        self.err = err
        self.interactive = interactive
        self.wait_healthy = wait_healthy or http_wait_healthy
        self.ask = ask

    def say(self, text: str) -> None:
        print(text, file=self.out, flush=True)

    def warn(self, text: str) -> None:
        print(text, file=self.err, flush=True)

    # -- helpers ---------------------------------------------------------------

    def state(self) -> str | None:
        """The container's status (running, exited, ...), or None if it doesn't exist."""
        code, text = self.runner(
            # --type container: the image has the same name, and must not answer for a missing container.
            ["podman", "inspect", "--type", "container", "--format", "{{.State.Status}}", CONTAINER], capture=True)
        return text.strip() if code == 0 else None

    def env_file(self, given: str | None) -> Path:
        """--env-file, then OS_ENV_FILE, then the repo root's .env.

        An explicit path must exist. Only the repo root is searched, never the
        current directory or its parents: run from another project's folder, a
        cwd search would hand that project's .env, secrets included, to the
        container. A discovered path is printed; an explicit one isn't echoed.

        An env file inside the repo is fine (unlike a db-snapshot): .gitignore and
        .dockerignore both exclude .env and *.env at any depth, so it can't reach
        git or the image, and the tool never writes or copies it.
        """
        value = given or self.env.get("OS_ENV_FILE")
        if value:
            # Absolute, because podman runs from the repo root (default_runner), not from here.
            path = Path(value).expanduser().resolve()
            if not path.is_file():
                raise DevError(f"env file not found: {path}")
            # Only .env and *.env are ignored by git and the image build; any other name in the
            # repo could be committed, or copied into the image from under src/.
            if inside_repo(path) and not path.name.endswith(".env"):
                self.warn(f"warning: {path.name} is inside the repository but isn't named .env or "
                          "*.env, so git and the image build don't ignore it")
            return path
        candidate = REPO / ".env"
        if candidate.is_file():
            self.warn(f"env file: {candidate}")  # the path only, never the contents
            return candidate
        raise DevError("no env file: pass --env-file PATH, set OS_ENV_FILE, "
                       "or put a .env at the repo root")

    def volume(self) -> str:
        return self.env.get("OS_VOLUME") or DEFAULT_VOLUME

    def exec_flags(self) -> list[str]:
        # -t only when stdin is a terminal: osctl's hidden prompts need it; piped stdin breaks with it.
        return ["-it"] if self.interactive() else ["-i"]

    def env_host_off(self) -> list[str]:
        """--env-host=false where this podman accepts it, so a containers.conf with
        env_host = true can't copy this shell, bot token included, into the container.
        Podman 5.8's Windows remote client rejects the flag ("unknown flag"); there the
        VM never sees this shell's environment (checked in MALU-221)."""
        code, text = self.runner(["podman", "run", "--help"], capture=True)
        return ["--env-host=false"] if code == 0 and "--env-host" in text else []

    def commit_tag(self) -> str | None:
        code, sha = self.runner(["git", "rev-parse", "--short", "HEAD"], capture=True)
        if code != 0 or not sha.strip():
            return None
        _, dirty = self.runner(["git", "status", "--porcelain"], capture=True)
        return sha.strip() + ("-dirty" if dirty.strip() else "")

    # -- commands --------------------------------------------------------------

    def build(self) -> int:
        # --format docker: the default OCI format has no HEALTHCHECK, so Podman would drop it.
        cmd = ["podman", "build", "--format", "docker", "-f", "deploy/Dockerfile", "-t", IMAGE]
        tag = self.commit_tag()
        if tag:
            cmd += ["-t", f"{IMAGE}:{tag}"]
        return self.runner(cmd + ["."])[0]

    def up(self, env_file: str | None, build: bool = False) -> int:
        path = self.env_file(env_file)
        self.check_env(path)
        if build:
            code = self.build()
            if code:
                return code
            if self.state() == "running":
                self.say(f"built, but {CONTAINER} is still running the old image; "
                         "`osctl container restart` switches to the new one")
                return 0
        return self.start(path)

    def check_env(self, path: Path) -> None:
        """Read the env file (key names only) before anything is stopped or started.

        Raises DevError if it can't be read, sets a key the bridge refuses, has a "*" in a
        key, starts with a UTF-8 BOM, or has a space before an "=". Warns when it sets keys the image owns:
        an env file written for a bridge outside the container overrides them with host values, and the
        container then can't start or can't be reached.
        """
        lines = numbered_env_lines(path)
        keys = {key.strip() for _, key, _, _ in lines}
        # Any case: Windows looks up environment names case-insensitively, so a bare
        # "discord_bot_token" is filled from the shell's DISCORD_BOT_TOKEN (checked against
        # Podman 5.8), and the value lands in the container's config under that name.
        refused = sorted(REFUSED_KEYS & {key.upper() for key in keys})
        if refused:
            raise DevError(f"{path} sets {', '.join(refused)}, which the bridge refuses; remove "
                           "it (the bot token belongs only in the shell that runs `osctl command`)")
        # Podman expands a "*" in a bare key from the host environment ("DISCORD_*", or "*"
        # for all of it), so a bot token in this shell would land in the container's config,
        # past the check above (checked against Podman 5.8 in MALU-206). Docker has no wildcard.
        # Here and below, line numbers only: on a malformed line, the text could be a value.
        wild = [number for number, key, _, _ in lines if "*" in key]
        if wild:
            raise DevError(f"{path} has a \"*\" in a key on line {', '.join(map(str, wild))}, which "
                           "Podman expands from this shell's environment; name each key")
        # Podman refuses "=value" only at `podman run`, after restart has stopped the bridge,
        # and its error prints the whole line, value included.
        empty = [number for number, key, sep, _ in lines if sep and not key.strip()]
        if empty:
            raise DevError(f"{path} has no key before \"=\" on line {', '.join(map(str, empty))}")
        # podman --env-file keeps a BOM as part of the first key, so the bridge would never
        # see that setting (checked against Podman 5.8 in MALU-206). Docker strips it.
        if starts_with_bom(path):
            raise DevError(f"{path} starts with a UTF-8 BOM, which Podman reads as part of the first "
                           "key; save it as UTF-8 without a BOM")
        # Podman also keeps a space before "=" in the key ("KEY =v" sets "KEY "), so that
        # setting is lost too. The checks above strip it, so they still see the key.
        spaced = [number for number, key, sep, _ in lines if sep and key != key.rstrip()]
        if spaced:
            raise DevError(f"{path} has a space before \"=\" on line {', '.join(map(str, spaced))}, which "
                           "Podman keeps as part of the key name; write KEY=value")
        clashes = sorted(IMAGE_OWNED_KEYS & keys)
        if clashes:
            self.warn(f"warning: {path.name} sets {', '.join(clashes)}, overriding the image's "
                      "container values; remove them if the container won't start or answer")

    def start(self, path: Path) -> int:
        state = self.state()
        if state == "running":
            # No new claim link: minting replaces the old one, which the owner may be signing in with.
            self.say(f"{CONTAINER} is already running; `osctl container restart` recreates it. "
                     "If the bridge isn't claimed yet, `osctl bridge claim` prints a claim link")
            return 0
        if state is not None:
            code = self.runner(["podman", "rm", CONTAINER])[0]
            if code:
                return code
        code = self.runner([
            "podman", "run", "-d", "--name", CONTAINER,
            # Never pull: an unqualified name would otherwise be looked up in a registry.
            "--pull", "never",
            *self.env_host_off(),
            "-p", PUBLISH,
            "-v", f"{self.volume()}:{DATA_DIR}",
            "--env-file", str(path),
            IMAGE,
        ])[0]
        if code:
            return code
        if self.wait_healthy():
            self.say(f"{CONTAINER} is up on {UPSTREAM}; `osctl container tunnel` in another terminal")
            self.claim_link()
            return 0
        self.warn(f"{CONTAINER} started but {HEALTHZ} isn't answering; try `osctl container logs`")
        return 1

    def claim_link(self) -> None:
        """On an unclaimed bridge, print a claim link (DESIGN.md, Local development).

        The container's own osctl mints it through the admin API, so the link goes from there
        straight to this terminal, never through the logs. `--if-unclaimed` keeps it quiet
        when there's nothing to claim. The container is up either way, so a failure only warns.
        The health check probes the public listener, which binds just before the admin one;
        `podman exec` takes far longer than that gap, and losing the race only warns.
        """
        code = self.runner(["podman", "exec", CONTAINER, "osctl", "bridge", "claim", "--if-unclaimed"])[0]
        if code:
            self.warn("no claim link was made; `osctl bridge claim` tries again")

    def down(self) -> int:
        if self.state() is None:
            self.say(f"{CONTAINER} isn't there")
            return 0
        code = self.runner(["podman", "stop", CONTAINER])[0]
        if code:
            return code
        # The volume is kept: it is the container's database.
        return self.runner(["podman", "rm", CONTAINER])[0]

    def restart(self, env_file: str | None, build: bool = False) -> int:
        # Check the env file and build before stopping anything, so a mistake
        # or a failed build leaves the running bridge up.
        path = self.env_file(env_file)
        self.check_env(path)
        if build:
            code = self.build()
            if code:
                return code
        code = self.down()
        return code or self.start(path)

    def status(self) -> int:
        code, text = self.runner([
            "podman", "inspect", "--type", "container", "--format",
            "{{.State.Status}} {{if .State.Health}}{{.State.Health.Status}}{{end}} "
            "{{.Config.Image}} {{range .Mounts}}{{.Name}} {{end}}",
            CONTAINER,
        ], capture=True)
        if code != 0:
            self.say(f"{CONTAINER}: not created")
            return 1
        status, _, rest = text.strip().partition(" ")
        self.say(f"{CONTAINER}: {status} {rest}".rstrip())
        if status != "running":
            return 1
        return self.osctl(["bridge", "status"])

    def logs(self, follow: bool, tail: str | None) -> int:
        cmd = ["podman", "logs"]
        if follow:
            cmd.append("-f")
        if tail:
            cmd += ["--tail", tail]
        return self.runner(cmd + [CONTAINER])[0]

    def osctl(self, args: list[str]) -> int:
        """An osctl API-group command, run by the container's own osctl. Nothing from
        this shell's environment goes along: podman exec adds none (checked in MALU-221)."""
        self.warn(f"osctl: running in container {CONTAINER}")
        return self.runner(["podman", "exec", *self.exec_flags(), CONTAINER, "osctl", *args])[0]

    def exec(self, args: list[str]) -> int:
        return self.runner(["podman", "exec", *self.exec_flags(), CONTAINER, *(args or ["sh"])])[0]

    def tunnel(self, url: str | None, env_file: str | None) -> int:
        if not url:
            url = public_base_url(self.env_file(env_file), self.env)
        if not url:
            raise DevError("no tunnel URL: pass --url, or set PUBLIC_BASE_URL in the env file")
        url = check_tunnel_url(url)
        # Discord's endpoint, the OAuth2 redirect and grok.com's connector are set to
        # PUBLIC_BASE_URL, and Funnel can only serve this machine's own name.
        host = self.funnel_host()
        if url != f"https://{host}":
            raise DevError(f"the tunnel URL (PUBLIC_BASE_URL or --url) isn't this machine's Funnel URL, "
                           f"https://{host}. Set PUBLIC_BASE_URL to that, and point Discord's Interactions "
                           "endpoint, the OAuth2 redirect and grok.com's connector at it too")
        # Foreground, not --bg: Funnel stays on only while this runs, and Ctrl-C turns
        # it off. `down` never touches Funnel, so it can't turn off a --bg Funnel that
        # a real deployment on this machine relies on (DESIGN.md, Local development).
        try:
            return self.runner(["tailscale", "funnel", UPSTREAM])[0]
        except KeyboardInterrupt:  # Ctrl-C is the normal way to stop, not an error
            self.say("Funnel stopped")
            return 0

    def funnel_host(self) -> str:
        """This machine's <machine>.<tailnet>.ts.net name, from `tailscale status --json`.

        The JSON also lists peers and addresses; only Self.DNSName is kept.
        """
        code, text = self.runner(["tailscale", "status", "--json"], capture=True)
        if code != 0:
            raise DevError("`tailscale status` failed: is Tailscale installed and running?")
        try:
            status = json.loads(text)
        except ValueError:
            raise DevError("`tailscale status --json` didn't return JSON") from None
        if not isinstance(status, dict) or status.get("BackendState") != "Running":
            raise DevError("Tailscale isn't connected: run `tailscale up` and log in")
        me = status.get("Self")
        me = me if isinstance(me, dict) else {}
        name = str(me.get("DNSName") or "").rstrip(".").lower()
        if not name.endswith(".ts.net"):
            raise DevError("this machine has no ts.net name: turn on MagicDNS and HTTPS "
                           "certificates for the tailnet")
        return name

    def snapshot_dir(self, out_dir: str | None) -> Path:
        # Resolved before the repo check: podman runs from the repo root (default_runner),
        # so a relative path would be checked here but written there.
        target = Path(out_dir or self.env.get("OS_SNAPSHOT_DIR") or DEFAULT_SNAPSHOT_DIR).expanduser().resolve()
        if inside_repo(target):
            raise DevError("refusing to write a snapshot inside the repository: it holds every whsec_")
        return target

    def db_snapshot(self, out_dir: str | None) -> int:
        target = self.snapshot_dir(out_dir)
        if self.state() != "running":
            raise DevError(f"{CONTAINER} isn't running")
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        name = datetime.datetime.now().strftime("bridge-%Y%m%d-%H%M%S-%f.db")  # podman cp overwrites
        inner = f"/tmp/{name}"
        try:
            code = self.runner(["podman", "exec", CONTAINER, "python", "-c", SNAPSHOT_SCRIPT, inner])[0]
            if code:
                return code
            dest = target / name
            with private_files():
                code = self.runner(["podman", "cp", f"{CONTAINER}:{inner}", str(dest)])[0]
            if code:
                return code
        finally:
            self.runner(["podman", "exec", CONTAINER, "rm", "-f", inner])
        if not dest.is_file():
            raise DevError(f"podman cp reported success but {dest} isn't there")
        # POSIX gets 0600. On Windows, mode bits don't apply and the default
        # directory (under the user profile) relies on the profile's ACL.
        if os.name != "nt":
            dest.chmod(0o600)
        self.say(f"snapshot: {dest}")
        self.say("It holds every whsec_ and automation URL. Keep it like the bot token.")
        self.warn_if_no_members(dest)
        return 0

    def reset(self, env_file: str | None, build: bool = False, snapshot: bool = True, yes: bool = False) -> int:
        """Delete the data volume and start on an empty database (DESIGN.md, Local development).

        Every check runs before anything is stopped: the env file, the snapshot directory,
        the confirmation, and the build. The old volume is exported after the bridge stops,
        so the copy is consistent even when the old database kept it from starting, and a
        failed export deletes nothing.
        """
        path = self.env_file(env_file)
        self.check_env(path)
        volume = self.volume()
        target = self.snapshot_dir(None) if snapshot else None
        # Said every time, so a scripted --yes run still logs what it deleted.
        self.say(f"This deletes the volume {volume}: every member, webhook and connector connection in it."
                 + (f" A copy is exported to {target} first." if target else " No copy is kept (--no-snapshot)."))
        if not self.confirm("reset", volume, yes):
            return 1
        if build:
            code = self.build()
            if code:
                return code
        return self.down() or self.replace_volume(volume, target) or self.start(path)

    def restore(self, file: str, env_file: str | None, snapshot: bool = True, yes: bool = False) -> int:
        """Replace the data volume with a snapshot and start on it (DESIGN.md, Local development).

        Takes a volume export from reset (.tar) or a db-snapshot copy (.db). Every check runs
        before anything is stopped: the file, the env file, the image, the snapshot directory,
        the confirmation, and the snapshot's schema, which the image checks in a scratch volume, so a
        mismatch is a message here rather than a crash-looping container. Nothing is migrated.
        """
        source = Path(file).expanduser().resolve()
        if not source.is_file():
            raise DevError(f"snapshot not found: {source}")
        if source.suffix not in (".tar", ".db"):
            raise DevError("restore takes a .tar from `osctl container reset` or a .db from "
                           "`osctl container db-snapshot`")
        path = self.env_file(env_file)
        self.check_env(path)
        # Every check of the snapshot runs in the image, so without one nothing could load it.
        if self.runner(["podman", "image", "exists", IMAGE])[0] != 0:
            raise DevError(f"there's no {IMAGE} image to check the snapshot with: run `osctl container build`")
        volume = self.volume()
        target = self.snapshot_dir(None) if snapshot else None
        self.say(f"This replaces the volume {volume} with {source.name}: every member, webhook and connector "
                 "connection now in it is lost, and the snapshot's come back: members removed, secrets rotated, "
                 "connector connections revoked and web sessions signed out since it was made, and whether "
                 "the bridge is claimed."
                 + (f" A copy is exported to {target} first." if target else " No copy is kept (--no-snapshot)."))
        if not self.confirm("restore", volume, yes):
            return 1
        # Both loads read one private copy, so the volume gets exactly what was checked even if the
        # file changes meanwhile. It holds every whsec_; TemporaryDirectory is 0700 on POSIX.
        with tempfile.TemporaryDirectory(prefix="outer-steppes-restore-") as tmp:
            if source.suffix == ".tar":
                tar = Path(tmp) / "volume.tar"
                with private_files():
                    shutil.copyfile(source, tar)
            else:
                tar = db_to_tar(source, Path(tmp))
            self.check_snapshot(source.name, tar)
            code = self.down() or self.replace_volume(volume, target, tar)
        return code or self.start(path)

    def confirm(self, command: str, volume: str, yes: bool) -> bool:
        """The volume's name typed on a terminal, or --yes. False: not confirmed, nothing changed."""
        if yes:
            return True
        if not self.interactive():
            raise DevError(f"{command} deletes the volume {volume} and every member in it: run it on a "
                           "terminal to confirm, or pass --yes")
        try:
            answer = self.ask(f"Type {volume} to confirm: ")
        except EOFError:  # Ctrl-D: nothing has changed yet
            answer = ""
        if answer.strip() != volume:
            self.say("not confirmed; nothing changed")
            return False
        return True

    def replace_volume(self, volume: str, target: Path | None, tar: Path | None = None) -> int:
        """With the container stopped: export the volume to target (None: no copy), remove it,
        then, given a tar, load that into a new volume of the same name. A failed check or
        export deletes nothing."""
        # `podman volume exists` exits 1 for a missing volume; anything else is an error.
        code = self.runner(["podman", "volume", "exists", volume])[0]
        if code not in (0, 1):
            raise DevError(f"couldn't check whether {volume} exists, so nothing was deleted; "
                           f"{CONTAINER} is stopped, and `osctl container up` starts it again")
        exists = code == 0
        exported = exists and target is not None
        if exported:
            code = self.export_volume(volume, target)
            if code:
                return code
        if exists:
            code = self.runner(["podman", "volume", "rm", volume])[0]
            if code:
                self.warn(f"removing {volume} failed (is another container using it?); {CONTAINER} is "
                          "stopped, and `osctl container up` starts it again on the old data")
                return code
        if tar is not None:
            code = self.load_volume(volume, tar)
            if code:
                self.warn(f"loading the snapshot into {volume} failed, so {CONTAINER} wasn't started; "
                          "run the same `osctl container restore` again"
                          + (". The snapshot exported above holds the replaced data; the one the next run "
                             "exports is only this half-loaded volume" if exported else ""))
                return code
        return 0

    def export_volume(self, volume: str, target: Path) -> int:
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        dest = target / datetime.datetime.now().strftime(f"{volume}-%Y%m%d-%H%M%S-%f.tar")
        with private_files():
            code = self.runner(["podman", "volume", "export", volume, "--output", str(dest)])[0]
        if code or not dest.is_file():
            dest.unlink(missing_ok=True)  # a partial export can still hold secrets
            self.warn(f"exporting {volume} failed, so nothing was deleted; {CONTAINER} is stopped, "
                      "and `osctl container up` starts it again")
            return code or 1
        if os.name != "nt":
            dest.chmod(0o600)
        self.say(f"snapshot: {dest}. `osctl container restore <file>` restores it")
        self.say("It holds every whsec_ and automation URL. Keep it like the bot token.")
        self.warn_if_no_members(dest)
        return 0

    def warn_if_no_members(self, snapshot: Path) -> None:
        """Say so when a new snapshot holds no members, since it then has nothing to restore.
        A warning only: the owner chose it over a prompt on --no-snapshot or --yes (MALU-257)."""
        try:
            members = member_count(snapshot)
        except DevError as exc:
            self.warn(f"warning: couldn't check whether the snapshot holds any members: {exc}")
            return
        if members == 0:
            self.warn("warning: the snapshot holds no members, so it has nothing to restore")

    def check_snapshot(self, name: str, tar: Path) -> None:
        """Load the snapshot into a scratch volume and check its schema there with the image's
        own code, while the bridge still runs on its volume. Raises DevError on any problem."""
        scratch = f"{self.volume()}-restore-check"
        self.remove_scratch(scratch)  # left over from an interrupted restore
        try:
            if self.load_volume(scratch, tar, scratch=True):
                raise DevError(f"loading {name} into a scratch volume failed, so nothing changed")
            code, text = self.runner(self.throwaway(scratch, "python") + [
                "-c", SCHEMA_CHECK_SCRIPT, f"{DATA_DIR}/{DB_NAME}"], capture=True)
            if code == 3:
                raise DevError(f"{name} doesn't match this image's schema ({text.strip()}), so nothing "
                               "changed. Restore doesn't migrate: use a snapshot this version made")
            if code:
                raise DevError(f"checking {name} failed, so nothing changed")
        finally:
            # Made just now, so it's ours without asking for the label: a failed lookup here
            # mustn't replace the real error.
            self.remove_scratch(scratch, ours=True)

    def remove_scratch(self, scratch: str, ours: bool = False) -> None:
        """Remove restore's scratch volume if it's there. A volume of that name without restore's
        label belongs to someone else: it's refused, never deleted."""
        # Captured here and in load_volume only to keep podman from echoing the volume's name.
        if self.runner(["podman", "volume", "exists", scratch])[0] == 0:
            if not ours:
                _, label = self.runner(["podman", "volume", "inspect", "--format",
                                        f'{{{{index .Labels "{SCRATCH_LABEL}"}}}}', scratch], capture=True)
                if label.strip() != "1":
                    raise DevError(f"a volume named {scratch} exists but restore didn't make it, so nothing "
                                   "changed; remove or rename it, or set OS_VOLUME to another name")
            if self.runner(["podman", "volume", "rm", scratch], capture=True)[0]:
                # It holds every whsec_ the snapshot does, so it mustn't stay unnoticed.
                self.warn(f"warning: couldn't remove the scratch volume {scratch}, which holds the snapshot's "
                          f"secrets; remove it with `podman volume rm {scratch}`")

    def load_volume(self, volume: str, tar: Path, scratch: bool = False) -> int:
        """Create the volume, import the tar, then give everything in it to the image's user,
        with no group or other access, as the bridge requires. A scratch volume is labeled, so
        only restore's own is ever removed as a leftover."""
        label = ["--label", f"{SCRATCH_LABEL}=1"] if scratch else []
        for cmd, capture in ((["podman", "volume", "create", *label, volume], True),
                             (["podman", "volume", "import", volume, str(tar)], False),
                             (self.throwaway(volume, "sh", root=True) + ["-c", FIX_OWNER_SCRIPT], False)):
            code = self.runner(cmd, capture=capture)[0]
            if code:
                return code
        return 0

    def throwaway(self, volume: str, entrypoint: str, root: bool = False) -> list[str]:
        """`podman run` for one command in the image with the volume mounted: removed when it
        exits, never pulled, no network, and none of this shell's environment."""
        return ["podman", "run", "--rm", "--pull", "never", *self.env_host_off(), "--network", "none",
                *(["--user", "0"] if root else []), "-v", f"{volume}:{DATA_DIR}",
                "--entrypoint", entrypoint, IMAGE]


def member_count(snapshot: Path) -> int:
    """Rows in registrations in a .db copy or a .tar volume export. Read from a private
    temporary copy, so the snapshot is never written to or left with a -shm beside it.
    Raises DevError if it can't be read."""
    with tempfile.TemporaryDirectory(prefix="outer-steppes-count-") as tmp:
        copy = Path(tmp) / DB_NAME
        try:
            with private_files():
                if snapshot.suffix == ".tar":
                    extract_db(snapshot, Path(tmp))
                else:
                    shutil.copyfile(snapshot, copy)
            if not copy.is_file():
                raise DevError(f"it has no {DB_NAME}")
            # A plain path, not a file: URI, which a "%" or "#" in the temp path would break; the copy
            # exists, so nothing is created. closing(): on Windows an open connection would keep the
            # directory from being removed.
            with contextlib.closing(sqlite3.connect(str(copy))) as conn:
                return conn.execute("SELECT count(*) FROM registrations").fetchone()[0]
        except (OSError, tarfile.TarError, sqlite3.Error) as exc:
            raise DevError(str(exc)) from None


def extract_db(tar_path: Path, into: Path) -> None:
    """The database and its -wal from a volume export, written under fixed names: no path
    from the archive is used, so nothing can land outside `into`."""
    wanted = {DB_NAME, DB_NAME + "-wal"}
    with tarfile.open(tar_path) as tar:
        for member in tar:
            name = posixpath.normpath(member.name).lstrip("/")
            if name in wanted and member.isfile():
                with tar.extractfile(member) as src, (into / name).open("wb") as dst:
                    shutil.copyfileobj(src, dst)


def db_to_tar(db_path: Path, into: Path) -> Path:
    """A db-snapshot copy as the tar `podman volume import` takes, holding only the database."""
    tar_path = into / "volume.tar"
    with private_files(), tarfile.open(tar_path, "w") as tar:
        info = tar.gettarinfo(str(db_path), arcname=DB_NAME)
        info.mode = 0o600
        with db_path.open("rb") as f:
            tar.addfile(info, f)
    return tar_path


def env_lines(env_file: Path) -> list[tuple[str, str, str]]:
    """(key, "=" or "", value) per line. Callers keep only what they need and print no values."""
    return [(key.strip(), sep, value.strip()) for _, key, sep, value in numbered_env_lines(env_file)]


def numbered_env_lines(env_file: Path) -> list[tuple[int, str, str, str]]:
    """(1-based line number, key, "=" or "", value) per setting, key and value unstripped
    except the line's outer whitespace, which Podman trims too."""
    try:
        # utf-8-sig: read past a BOM, so the refused-key check still sees a first-line key.
        # check_env then refuses the file, because Podman keeps the BOM in that key.
        text = env_file.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError:
        raise DevError(f"{env_file} isn't UTF-8 (PowerShell 5's Out-File writes UTF-16); "
                       "save it as UTF-8, as podman --env-file expects") from None
    except OSError as exc:
        raise DevError(f"can't read {env_file}: {exc.strerror}") from None
    lines = []
    # split("\n"), not splitlines(): Podman breaks lines only at "\n", so the numbers match.
    for number, line in enumerate(text.split("\n"), 1):
        line = line.strip()
        if line and not line.startswith("#"):
            lines.append((number, *line.partition("=")))
    return lines


def starts_with_bom(env_file: Path) -> bool:
    with env_file.open("rb") as f:
        return f.read(3) == codecs.BOM_UTF8


def public_base_url(env_file: Path, host_env) -> str | None:
    """Only PUBLIC_BASE_URL is kept; nothing else in the file is kept or printed."""
    found = None
    for key, sep, value in env_lines(env_file):
        if key == "PUBLIC_BASE_URL":
            # The last one wins, as in the container. A bare key (no "=") means
            # podman takes the value from the host environment.
            found = value if sep else host_env.get("PUBLIC_BASE_URL")
    return found


def check_tunnel_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    try:
        port = parts.port
    except ValueError:
        port = "bad"
    if parts.scheme != "https" or not parts.hostname or parts.path not in ("", "/") \
            or parts.query or parts.fragment or parts.username or port is not None:
        raise DevError("the tunnel URL must be a bare https origin, like https://<machine>.<tailnet>.ts.net")
    return f"https://{parts.hostname}"


def http_wait_healthy(timeout: float = 20.0) -> bool:
    # No proxies: a system or HTTP_PROXY proxy would take this loopback probe off the machine.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + timeout
    while True:
        try:
            with opener.open(HEALTHZ, timeout=2) as response:
                if response.status == 200:
                    return True
        except (urllib.error.URLError, OSError):
            pass
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.5)


def build_parser(prog: str = "osctl_container.py") -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog=prog, description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("build")
    for name in ("up", "restart"):
        p = sub.add_parser(name)
        p.add_argument("--build", action="store_true", help="build the image first")
        p.add_argument("--env-file", help="defaults to OS_ENV_FILE, then .env")
    sub.add_parser("down")
    sub.add_parser("status")
    p = sub.add_parser("logs")
    p.add_argument("-f", "--follow", action="store_true")
    p.add_argument("--tail")
    # Listed for --help only: run() hands its arguments over untouched.
    sub.add_parser("exec", help="CMD... inside the container (default: sh)")
    p = sub.add_parser("tunnel")
    p.add_argument("--url", help="defaults to PUBLIC_BASE_URL from the env file")
    p.add_argument("--env-file", help="defaults to OS_ENV_FILE, then .env")
    sub.add_parser("db-snapshot").add_argument("--out", help="defaults to OS_SNAPSHOT_DIR")
    p = sub.add_parser("reset", help="delete the data volume and start on an empty database")
    p.add_argument("--build", action="store_true", help="build the image first")
    p.add_argument("--no-snapshot", action="store_true", help="don't export the old volume first")
    p.add_argument("--yes", action="store_true", help="don't ask; needed without a terminal")
    p.add_argument("--env-file", help="defaults to OS_ENV_FILE, then .env")
    p = sub.add_parser("restore", help="replace the data volume with a snapshot and start on it")
    p.add_argument("file", help="a .tar from reset or a .db from db-snapshot")
    p.add_argument("--no-snapshot", action="store_true", help="don't export the current volume first")
    p.add_argument("--yes", action="store_true", help="don't ask; needed without a terminal")
    p.add_argument("--env-file", help="defaults to OS_ENV_FILE, then .env")
    return parser


def run(argv: list[str], dev: Dev, prog: str = "osctl_container.py") -> int:
    try:
        # Passed through before argparse, which would claim options like --help.
        if argv[:1] == ["exec"]:
            return dev.exec(argv[1:])
        args = build_parser(prog).parse_args(argv)
        if args.command == "build":
            return dev.build()
        if args.command == "up":
            return dev.up(args.env_file, build=args.build)
        if args.command == "restart":
            return dev.restart(args.env_file, build=args.build)
        if args.command == "down":
            return dev.down()
        if args.command == "status":
            return dev.status()
        if args.command == "logs":
            return dev.logs(args.follow, args.tail)
        if args.command == "tunnel":
            return dev.tunnel(args.url, args.env_file)
        if args.command == "db-snapshot":
            return dev.db_snapshot(args.out)
        if args.command == "reset":
            return dev.reset(args.env_file, build=args.build, snapshot=not args.no_snapshot, yes=args.yes)
        if args.command == "restore":
            return dev.restore(args.file, args.env_file, snapshot=not args.no_snapshot, yes=args.yes)
    except DevError as exc:
        dev.warn(f"{prog}: {exc}")
        return 2
    raise AssertionError(args.command)


def main(argv: list[str], prog: str = "osctl_container.py") -> int:
    return run(argv, Dev(), prog)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
