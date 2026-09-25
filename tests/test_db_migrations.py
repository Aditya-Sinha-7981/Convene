"""Migration runner, connection settings, and agreement between migration 0001 and docs/data-model.md."""
import re
import shutil
import sqlite3

import pytest

from server.db import MIGRATIONS_DIR, Database, connect, discover_migrations, migrate
from server.errors import DatabaseOpenError, MigrationError, SchemaVersionError
from tests.test_api_contract_docs import ENTITY_FIELDS, ENUMS

CORE_TABLES = {"Meeting", "Device", "Participant", "Utterance", "ConnectionEvent", "AuditEvent"}
LATEST_VERSION = 4  # 0001 core, 0002 ModelExecution, 0003/4 transcript chunks (CON-08)
TABLES_SO_FAR = CORE_TABLES | {"ModelExecution", "TranscriptChunk", "TranscriptIndexMeta", "TranscriptChunkVector"}


def schema(conn):
    return {r["name"]: r["sql"] for r in conn.execute("SELECT name, sql FROM sqlite_master WHERE name NOT LIKE 'sqlite_%'")}


def tables(conn):
    # sqlite-vec creates implementation tables alongside its declared virtual table.
    return {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' AND name NOT LIKE 'sqlite_%'")
            if not r[0].startswith("TranscriptChunkVector_")}


def test_creates_the_schema_from_an_empty_file(tmp_path):
    database = Database.open(tmp_path / "new.db")
    assert database.conn.execute("PRAGMA user_version").fetchone()[0] == LATEST_VERSION
    assert tables(database.conn) == TABLES_SO_FAR  # each task adds only the tables it first uses
    database.close()


def test_connection_pragmas(tmp_path):
    conn = connect(tmp_path / "p.db", busy_timeout_ms=1234)
    assert conn.execute("PRAGMA foreign_keys").fetchone()[0] == 1
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] == 1234
    assert conn.row_factory is sqlite3.Row
    conn.close()


def test_second_run_is_a_no_op_and_reopening_keeps_data(tmp_path):
    path = tmp_path / "idem.db"
    first = Database.open(path)
    before = schema(first.conn)
    first.conn.execute("INSERT INTO Meeting (meeting_id, title, status, created_at) VALUES (?, 't', 'created', ?)",
                       ("0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8", "2026-09-21T11:30:00.000Z"))
    assert migrate(first.conn) == LATEST_VERSION
    assert schema(first.conn) == before
    first.close()
    second = Database.open(path)
    assert schema(second.conn) == before
    assert second.conn.execute("SELECT COUNT(*) FROM Meeting").fetchone()[0] == 1
    second.close()


def test_failed_migration_rolls_back_completely(tmp_path):
    good = tmp_path / "migrations"
    good.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_core.sql", good / "0001_core.sql")
    (good / "0002_bad.sql").write_text(
        "CREATE TABLE Later (id TEXT PRIMARY KEY);\n"
        "INSERT INTO Later VALUES ('a');\n"
        "CREATE TABLE Later (id TEXT PRIMARY KEY);\n")  # duplicate table -> fails after two good statements
    path = tmp_path / "rb.db"
    conn = connect(path)
    # apply only 0001 first, so the database sits at version 1
    only_first = tmp_path / "only_first"
    only_first.mkdir()
    shutil.copy(MIGRATIONS_DIR / "0001_core.sql", only_first / "0001_core.sql")
    assert migrate(conn, only_first) == 1

    with pytest.raises(MigrationError, match="0002_bad.sql"):
        migrate(conn, good)
    assert conn.execute("PRAGMA user_version").fetchone()[0] == 1
    assert "Later" not in tables(conn)
    assert not conn.in_transaction
    conn.close()


def test_migration_files_must_be_named_and_numbered_contiguously(tmp_path):
    (tmp_path / "0001_a.sql").write_text("SELECT 1;")
    (tmp_path / "0003_c.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="contiguous"):
        discover_migrations(tmp_path)
    (tmp_path / "0003_c.sql").unlink()
    (tmp_path / "notes.sql").write_text("SELECT 1;")
    with pytest.raises(MigrationError, match="must look like"):
        discover_migrations(tmp_path)
    assert [n for n, _ in discover_migrations(MIGRATIONS_DIR)] == list(range(1, LATEST_VERSION + 1))


def test_database_newer_than_the_code_stops_startup(tmp_path):
    path = tmp_path / "newer.db"
    conn = connect(path)
    conn.execute("PRAGMA user_version = 99")
    conn.close()
    with pytest.raises(SchemaVersionError, match="newer"):
        Database.open(path)


def test_corrupt_file_stops_startup_with_a_clear_error(tmp_path):
    path = tmp_path / "corrupt.db"
    path.write_bytes(b"this is not a sqlite database " * 200)
    with pytest.raises(DatabaseOpenError, match="cannot open"):
        Database.open(path)


def test_columns_match_the_data_model(db):
    for entity in CORE_TABLES:
        columns = {r["name"] for r in db.conn.execute(f"PRAGMA table_info({entity})")}
        # ``started_at`` and ``ended_at`` are documented nullable Meeting fields but API examples omit them.
        expected = ENTITY_FIELDS[entity] | ({"started_at", "ended_at"} if entity == "Meeting" else set())
        assert columns == expected, entity


def test_check_enums_match_the_documented_values(db):
    sql = schema(db.conn)
    checked = 0
    for (entity, column), documented in ENUMS.items():
        if entity not in CORE_TABLES or entity == "AuditEvent":  # audit values are enforced by emit()
            continue
        match = re.search(rf"\b{column}\s+IN\s*\(([^)]*)\)", sql[entity])
        assert match, f"{entity}.{column} has no CHECK ... IN (...)"
        assert set(re.findall(r"'([^']+)'", match.group(1))) == documented, f"{entity}.{column}"
        checked += 1
    assert checked >= 5  # Meeting.status, Device.status, Participant.enrollment_status, attribution_method, ConnectionEvent.event_type


def test_documented_indexes_exist_with_the_documented_columns(db):
    def index_columns(name):
        return [r["name"] for r in db.conn.execute(f"PRAGMA index_info({name})")]
    assert index_columns("idx_utterance_meeting_t_start") == ["meeting_id", "t_start"]
    assert index_columns("idx_utterance_meeting_participant") == ["meeting_id", "participant_id"]
    assert index_columns("idx_audit_meeting_timestamp") == ["meeting_id", "timestamp"]
    assert index_columns("idx_audit_type_timestamp") == ["event_type", "timestamp"]
    unique = [r for r in db.conn.execute("PRAGMA index_list(AuditEvent)") if r["unique"]]
    assert any(index_columns(r["name"]) == ["seq"] for r in unique)


def test_no_orm_or_migration_library_dependency():
    from pathlib import Path
    text = (Path(__file__).resolve().parents[1] / "requirements.txt").read_text().lower()
    for banned in ("sqlalchemy", "alembic", "peewee", "yoyo", "django"):
        assert banned not in text
