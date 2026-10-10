from __future__ import annotations

import os
import sqlite3
import stat

import pytest

from outer_steppes import db

DELETE_ALL_THREE = "delete it together with its -wal and -shm files"


def test_initialize_creates_the_schema(tmp_path):
    path = str(tmp_path / "new" / "bridge.db")
    db.initialize(path)
    conn = db.connect(path, 100)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == db.SCHEMA_VERSION == 1
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert tables == {"settings", "registrations", "interactions", "rate_events",
                      "oauth_clients", "oauth_codes", "oauth_tokens", "oauth_pending", "automations",
                      "member_settings", "web_sessions", "account_changes", "banned_secrets"}
    columns = {r["name"] for r in conn.execute("PRAGMA table_info(registrations)")}
    # The webhook lives in automations (one source of truth), not on the member.
    assert "automation_url" not in columns and "signing_secret" not in columns
    assert "last_used_at" in columns and "uses_connector" not in columns  # every answer uses the connector
    interactions = {r["name"] for r in conn.execute("PRAGMA table_info(interactions)")}
    assert "prompt_mac" in interactions and not any("excerpt" in c for c in interactions)  # no prompt text
    assert {"guild_installed", "automation_id"} <= {r["name"] for r in conn.execute("PRAGMA table_info(interactions)")}
    assert [r["name"] for r in conn.execute("PRAGMA table_info(web_sessions)")] == [
        "session_hash", "discord_user_id", "username", "expires_at"]  # no CSRF column: it isn't stored
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    conn.close()


def test_initialize_is_idempotent_and_keeps_data(db_path):
    db.initialize(db_path)
    conn = db.connect(db_path, 100)
    conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
    conn.close()
    db.initialize(db_path)
    conn = db.connect(db_path, 100)
    assert conn.execute("SELECT value FROM settings").fetchone()[0] == "1"
    conn.close()


# 8 is a development database from before the schema was collapsed (DESIGN.md, Data model).
@pytest.mark.parametrize("version", [2, 8, 9, 99])
def test_any_other_version_refuses_and_is_left_alone(db_path, version):
    conn = sqlite3.connect(db_path)
    conn.execute(f"PRAGMA user_version = {version}")
    conn.close()
    with pytest.raises(db.DatabaseError, match=f"version {version} is not supported .*{DELETE_ALL_THREE}"):
        db.initialize(db_path)
    conn = sqlite3.connect(db_path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == version
    conn.close()


def test_a_development_v1_database_refuses_and_is_left_alone(tmp_path):
    """The development schema also started at version 1 (PRs 2-4), with mcp_tokens and no OAuth tables."""
    db_path = str(tmp_path / "old.db")
    conn = sqlite3.connect(db_path)
    for table in ("settings", "registrations", "mcp_tokens", "interactions", "rate_events"):
        conn.execute(f"CREATE TABLE {table} (x)")
    conn.execute("PRAGMA user_version = 1")
    conn.close()
    with pytest.raises(db.DatabaseError, match=f"tables don't match this schema.*{DELETE_ALL_THREE}"):
        db.initialize(db_path)
    conn = sqlite3.connect(db_path)
    assert "mcp_tokens" in {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    conn.close()


def test_tables_without_version_refuse(tmp_path):
    path = str(tmp_path / "other.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE something_else (x)")
    conn.close()
    with pytest.raises(db.DatabaseError, match="no schema version"):
        db.initialize(path)


def test_a_failed_create_leaves_an_empty_file(tmp_path, monkeypatch):
    """The schema and its version commit together, so a retry starts from scratch."""
    path = str(tmp_path / "x.db")
    monkeypatch.setattr(db, "SCHEMA", db.SCHEMA + ";\nCREATE TABLE settings (x)")  # fails: settings exists
    with pytest.raises(sqlite3.OperationalError):
        db.initialize(path)
    conn = sqlite3.connect(path)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 0
    assert conn.execute("SELECT count(*) FROM sqlite_master WHERE type = 'table'").fetchone()[0] == 0
    conn.close()


def test_schema_constraints(conn):
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("INSERT INTO interactions (interaction_id, status, ack_kind, created_at, updated_at) "
                     "VALUES ('1', 'bogus', 'defer', 0, 0)")


def test_guild_installed_defaults_to_1_so_the_allowlist_applies(conn):
    """Fails closed: a row written without it still gets GUILD_ALLOWLIST at the re-checks (DESIGN.md, Data model)."""
    conn.execute("INSERT INTO interactions (interaction_id, status, ack_kind, created_at, updated_at) "
                 "VALUES ('1', 'claimed', 'ephemeral', 0, 0)")
    assert conn.execute("SELECT guild_installed FROM interactions").fetchone()[0] == 1


def test_transaction_rolls_back_on_error(conn):
    with pytest.raises(RuntimeError):
        with db.transaction(conn):
            conn.execute("INSERT INTO settings (key, value) VALUES ('paused', '1')")
            raise RuntimeError("boom")
    assert conn.execute("SELECT count(*) FROM settings").fetchone()[0] == 0


class FakeStat:
    def __init__(self, modes: dict[str, int]):
        self.modes = modes

    def __call__(self, target: str):
        if target not in self.modes:
            raise FileNotFoundError(target)
        return os.stat_result((stat.S_IFREG | self.modes[target], 0, 0, 0, 0, 0, 0, 0, 0, 0))


def modes(directory=0o700, file=0o600, wal=0o600, shm=0o600):
    result = {"/data": directory, "/data/b.db": file}
    if wal is not None:
        result["/data/b.db-wal"] = wal
    if shm is not None:
        result["/data/b.db-shm"] = shm
    return result


def test_file_modes_pass_when_private():
    db.check_file_modes("/data/b.db", is_posix=True, stat_fn=FakeStat(modes()))
    db.check_file_modes("/data/b.db", is_posix=True, stat_fn=FakeStat(modes(wal=None, shm=None)))


@pytest.mark.parametrize("kw", [
    {"directory": 0o755}, {"directory": 0o770}, {"file": 0o644}, {"file": 0o640},
    {"wal": 0o644}, {"shm": 0o604},
])
def test_file_modes_refuse_group_or_other_access(kw):
    with pytest.raises(db.DatabaseError, match="accessible to group or others"):
        db.check_file_modes("/data/b.db", is_posix=True, stat_fn=FakeStat(modes(**kw)))


def test_missing_database_file_refuses():
    fake = FakeStat({"/data": 0o700})
    with pytest.raises(db.DatabaseError, match="missing"):
        db.check_file_modes("/data/b.db", is_posix=True, stat_fn=fake)


def test_file_mode_check_is_skipped_off_posix():
    db.check_file_modes("/data/b.db", is_posix=False, stat_fn=FakeStat({}))


def test_old_sqlite_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(sqlite3, "sqlite_version_info", (3, 23, 1))
    with pytest.raises(db.DatabaseError, match="3.24.0 or later"):
        db.initialize(str(tmp_path / "x.db"))
