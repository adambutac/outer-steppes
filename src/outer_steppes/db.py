"""SQLite storage (DESIGN.md, Data model).

One file holds everything. It is secret material: the file, its -wal and
-shm siblings, and its directory must not be readable by group or others.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import sqlite3
import stat
from collections.abc import Callable, Iterator
from pathlib import Path

log = logging.getLogger(__name__)

SCHEMA_VERSION = 1
ACK_BUSY_TIMEOUT_MS = 200
BACKGROUND_BUSY_TIMEOUT_MS = 3000

# The whole schema (DESIGN.md, Data model). Nothing has been released, so there are
# no migrations: an empty file gets this, and any other version refuses to start.
# After the first release, a schema change ships with a migration again.
# No semicolons inside statements: _statements splits on them.
SCHEMA = """
CREATE TABLE settings (
  key    TEXT PRIMARY KEY,
  value  TEXT NOT NULL
);

CREATE TABLE registrations (
  id               TEXT PRIMARY KEY,
  discord_user_id  TEXT NOT NULL,
  label            TEXT,
  last_used_at     INTEGER,
  foreign_use_at   INTEGER,
  revoked_at       INTEGER,
  created_at       INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);
CREATE UNIQUE INDEX registrations_one_active
  ON registrations (discord_user_id) WHERE revoked_at IS NULL;

CREATE TABLE automations (
  id               TEXT PRIMARY KEY,
  registration_id  TEXT NOT NULL REFERENCES registrations(id) ON DELETE CASCADE,
  kind             TEXT NOT NULL CHECK (kind IN ('grok_webhook', 'grok_bot_webhook')),
  automation_url   TEXT,
  signing_secret   TEXT,
  secret_hash      TEXT,
  created_at       INTEGER NOT NULL,
  removed_at       INTEGER
);
CREATE UNIQUE INDEX automations_one_active ON automations (registration_id) WHERE removed_at IS NULL;
CREATE UNIQUE INDEX automations_one_url ON automations (automation_url);

CREATE TABLE banned_secrets (
  secret_hash      TEXT PRIMARY KEY,
  banned_at        INTEGER NOT NULL
);

CREATE TABLE member_settings (
  discord_user_id  TEXT PRIMARY KEY,
  answers_private  INTEGER NOT NULL,
  updated_at       INTEGER NOT NULL
);

CREATE TABLE oauth_clients (
  client_id        TEXT PRIMARY KEY,
  registration_id  TEXT NOT NULL REFERENCES registrations(id) ON DELETE CASCADE,
  secret_hash      TEXT NOT NULL,
  created_at       INTEGER NOT NULL,
  last_used_at     INTEGER
);
CREATE INDEX oauth_clients_registration ON oauth_clients (registration_id);

CREATE TABLE oauth_codes (
  code_hash        TEXT PRIMARY KEY,
  client_id        TEXT NOT NULL,
  registration_id  TEXT NOT NULL REFERENCES registrations(id) ON DELETE CASCADE,
  redirect_uri     TEXT NOT NULL,
  code_challenge   TEXT NOT NULL,
  resource         TEXT,
  expires_at       INTEGER NOT NULL
);

CREATE TABLE oauth_tokens (
  token_hash       TEXT PRIMARY KEY,
  kind             TEXT NOT NULL CHECK (kind IN ('access', 'refresh')),
  client_id        TEXT NOT NULL,
  registration_id  TEXT NOT NULL REFERENCES registrations(id) ON DELETE CASCADE,
  connection_id    TEXT NOT NULL,
  connected_at     INTEGER NOT NULL,
  expires_at       INTEGER NOT NULL,
  created_at       INTEGER NOT NULL,
  last_used_at     INTEGER
);
CREATE INDEX oauth_tokens_by_registration ON oauth_tokens (registration_id, connection_id);
CREATE INDEX oauth_tokens_client ON oauth_tokens (client_id);

CREATE TABLE oauth_pending (
  state_hash       TEXT PRIMARY KEY,
  nonce_hash       TEXT NOT NULL,
  purpose          TEXT NOT NULL CHECK (purpose IN ('connector', 'register', 'claim')),
  redirect_uri     TEXT,
  code_challenge   TEXT,
  resource         TEXT,
  client_state     TEXT,
  claim_hash       TEXT,
  expires_at       INTEGER NOT NULL,
  CHECK (purpose != 'connector' OR (redirect_uri IS NOT NULL AND code_challenge IS NOT NULL)),
  CHECK (purpose != 'claim' OR claim_hash IS NOT NULL)
);
CREATE INDEX oauth_pending_expiry ON oauth_pending (expires_at);

CREATE TABLE web_sessions (
  session_hash     TEXT PRIMARY KEY,
  discord_user_id  TEXT NOT NULL,
  username         TEXT NOT NULL,
  expires_at       INTEGER NOT NULL
);
CREATE INDEX web_sessions_user ON web_sessions (discord_user_id);
CREATE INDEX web_sessions_expiry ON web_sessions (expires_at);
CREATE TABLE account_changes (
  discord_user_id  TEXT NOT NULL,
  at               INTEGER NOT NULL
);
CREATE INDEX account_changes_user ON account_changes (discord_user_id, at);

CREATE TABLE interactions (
  interaction_id     TEXT PRIMARY KEY,
  status             TEXT NOT NULL CHECK (status IN
                       ('claimed','done','queued','posted','answering','answered','failed','expired')),
  ack_kind           TEXT NOT NULL CHECK (ack_kind IN ('defer','ephemeral')),
  ack_content        TEXT NOT NULL DEFAULT '',
  private            INTEGER NOT NULL DEFAULT 0,
  guild_id           TEXT,
  guild_installed    INTEGER NOT NULL DEFAULT 1,  -- fails closed: GUILD_ALLOWLIST applies when in doubt
  channel_id         TEXT,
  caller_id          TEXT,
  registration_id    TEXT,
  automation_id      TEXT,
  owner_id           TEXT,
  correlation_id     TEXT,
  prompt_mac         TEXT,
  interaction_token  TEXT,
  final_message      TEXT,
  chunks_sent        INTEGER NOT NULL DEFAULT 0,
  reply_token_hash   TEXT UNIQUE,
  reply_expires_at   INTEGER,
  post_outcome       TEXT CHECK (post_outcome IN ('pending','accepted','ambiguous') OR post_outcome IS NULL),
  posted_at          INTEGER,
  created_at         INTEGER NOT NULL,
  updated_at         INTEGER NOT NULL
);
CREATE INDEX interactions_location ON interactions (guild_id, channel_id, caller_id, status);
CREATE INDEX interactions_registration ON interactions (registration_id, status);
CREATE INDEX interactions_status ON interactions (status, reply_expires_at);
CREATE INDEX interactions_pending_final ON interactions (interaction_id) WHERE final_message IS NOT NULL;

CREATE TABLE rate_events (
  subject_kind TEXT NOT NULL CHECK (subject_kind IN ('caller','owner')),
  subject_id   TEXT NOT NULL,
  at           INTEGER NOT NULL
);
CREATE INDEX rate_events_subject ON rate_events (subject_kind, subject_id, at);
"""

# SCHEMA is the version 1 base. Migrations, when they come back, go on top of it.
BASE_TABLES = frozenset(re.findall(r"^CREATE TABLE (\w+)", SCHEMA, re.MULTILINE))
# A stale -wal next to a new file can replay the old pages into it, so all three go.
DELETE_OR_RESTORE = ("delete it together with its -wal and -shm files, "
                     "or restore a snapshot made by this version")

# ON CONFLICT upserts (commands.py, interactions.py, registry.py) need SQLite >= 3.24.
MIN_SQLITE = (3, 24, 0)


_warned_not_posix = False


class DatabaseError(Exception):
    """The database file is unusable: wrong version or unsafe permissions."""


def connect(path: str, busy_timeout_ms: int) -> sqlite3.Connection:
    # isolation_level=None: no implicit transactions. Every write goes through
    # transaction(), which issues BEGIN IMMEDIATE explicitly.
    conn = sqlite3.connect(path, isolation_level=None, timeout=busy_timeout_ms / 1000)
    conn.row_factory = sqlite3.Row
    conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA synchronous = NORMAL")
    return conn


@contextlib.contextmanager
def transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """BEGIN IMMEDIATE ... COMMIT, rolling back on any exception."""
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
    except BaseException:
        conn.execute("ROLLBACK")
        raise
    conn.execute("COMMIT")


def initialize(path: str) -> None:
    """Create the directory, create the schema on an empty file, and check the version."""
    if sqlite3.sqlite_version_info < MIN_SQLITE:
        raise DatabaseError(f"SQLite {sqlite3.sqlite_version} is too old; version "
                            f"{'.'.join(map(str, MIN_SQLITE))} or later is required")
    directory = Path(path).parent
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)

    conn = connect(path, BACKGROUND_BUSY_TIMEOUT_MS)
    try:
        conn.execute("PRAGMA journal_mode = WAL")
        version = conn.execute("PRAGMA user_version").fetchone()[0]
        if version == 1:
            # The development schema also started at 1 (PRs 2-4, before the reset), with
            # other tables. Same number, different schema: refuse it rather than fail later.
            # Pinned to 1, not SCHEMA_VERSION: once migrations exist, this still runs first.
            tables = {r[0] for r in conn.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")}
            if tables != BASE_TABLES:
                raise DatabaseError(
                    f"database is at schema version 1 but its tables don't match this schema (a development "
                    f"database from before the schema reset?); {DELETE_OR_RESTORE}"
                )
        if version == SCHEMA_VERSION:
            return
        if version != 0:
            # Every such version is a development database today, so deleting is right. The first
            # migration must split out version > SCHEMA_VERSION (a rollback): never advise deleting that.
            raise DatabaseError(
                f"database schema version {version} is not supported (expected {SCHEMA_VERSION}); "
                f"{DELETE_OR_RESTORE}"
            )
        has_tables = conn.execute(
            "SELECT count(*) FROM sqlite_master WHERE type = 'table'"
        ).fetchone()[0]
        if has_tables:
            raise DatabaseError("database has tables but no schema version; refusing to touch it")
        # One transaction for the schema and its version, so a failure leaves an
        # empty file. Not executescript(): it COMMITs any open transaction first.
        with transaction(conn):
            for statement in _statements(SCHEMA):
                conn.execute(statement)
            conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")
    finally:
        conn.close()


def _statements(script: str) -> list[str]:
    # The schema has no semicolons inside statements, so splitting is safe.
    return [s.strip() for s in script.split(";") if s.strip()]


def check_file_modes(
    path: str,
    *,
    is_posix: bool = os.name == "posix",
    stat_fn: Callable[[str], os.stat_result] = os.stat,
) -> None:
    """Refuse to run if the database or its directory is group/other accessible.

    POSIX permission bits are the control here. On Windows they do not
    describe access (ACLs do), so the check is skipped with a warning; the
    production target is a Linux VM.
    """
    if not is_posix:
        global _warned_not_posix
        if not _warned_not_posix:  # once per process, not on every /readyz
            log.warning("database permission checks skipped: not a POSIX system")
            _warned_not_posix = True
        return

    directory = os.path.dirname(path) or "."
    _require_mode(directory, stat_fn, 0o700, "directory")
    for candidate in (path, path + "-wal", path + "-shm"):
        try:
            _require_mode(candidate, stat_fn, 0o600, "file")
        except FileNotFoundError:
            if candidate == path:
                raise DatabaseError("database file is missing") from None


def _require_mode(target: str, stat_fn: Callable[[str], os.stat_result], allowed: int, kind: str) -> None:
    mode = stat.S_IMODE(stat_fn(target).st_mode)
    if mode & ~allowed & 0o077:
        raise DatabaseError(
            f"database {kind} {target!r} is accessible to group or others "
            f"(mode {mode:o}); expected {allowed:o}"
        )
