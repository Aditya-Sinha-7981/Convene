"""sqlite-vec spike (CON-03 Requirement 7): can the target Python load it, and does it offer what CON-08 needs?

Skipped, not failed, when the optional ``sqlite-vec`` package is not installed: CON-08 adds it to the
requirements. Results and versions are recorded in logs/persistence.md.
"""
import sqlite3
import struct

import pytest

sqlite_vec = pytest.importorskip("sqlite_vec")


def vec(*values):
    return struct.pack(f"{len(values)}f", *values)


@pytest.fixture
def conn():
    if not hasattr(sqlite3.Connection, "enable_load_extension"):
        pytest.fail("this Python's sqlite3 cannot load extensions; CON-08 needs a different interpreter")
    c = sqlite3.connect(":memory:")
    c.enable_load_extension(True)
    sqlite_vec.load(c)
    c.enable_load_extension(False)
    return c


def test_extension_loads_and_reports_its_version(conn):
    version = conn.execute("SELECT vec_version()").fetchone()[0]
    assert version.startswith("v0.")


def test_vec0_knn_query_returns_nearest_first(conn):
    conn.execute("CREATE VIRTUAL TABLE v USING vec0(embedding float[3])")
    conn.executemany("INSERT INTO v(rowid, embedding) VALUES (?, ?)",
                     [(1, vec(1, 0, 0)), (2, vec(0, 1, 0)), (3, vec(0.9, 0.1, 0))])
    rows = conn.execute("SELECT rowid, distance FROM v WHERE embedding MATCH ? AND k = 2 ORDER BY distance",
                        (vec(1, 0, 0),)).fetchall()
    assert [r[0] for r in rows] == [1, 3]


def test_vec0_supports_a_text_primary_key(conn):
    conn.execute("CREATE VIRTUAL TABLE v USING vec0(chunk_id text primary key, embedding float[3])")
    chunk_id = "2f9e6d13-a7c4-4b80-9e52-6d1b8a4c0f37"
    conn.execute("INSERT INTO v(chunk_id, embedding) VALUES (?, ?)", (chunk_id, vec(1, 0, 0)))
    assert conn.execute("SELECT chunk_id FROM v WHERE embedding MATCH ? AND k = 1", (vec(1, 0, 0),)).fetchone()[0] == chunk_id


def test_vec0_partition_key_filters_by_meeting_with_cosine_distance(conn):
    conn.execute("CREATE VIRTUAL TABLE v USING vec0(chunk_id text primary key, meeting_id text partition key, "
                 "embedding float[3] distance_metric=cosine)")
    conn.executemany("INSERT INTO v(chunk_id, meeting_id, embedding) VALUES (?, ?, ?)",
                     [("a1", "A", vec(1, 0, 0)), ("a2", "A", vec(0, 1, 0)), ("b1", "B", vec(1, 0, 0))])
    only_a = conn.execute("SELECT chunk_id FROM v WHERE embedding MATCH ? AND k = 5 AND meeting_id = 'A' ORDER BY distance",
                          (vec(1, 0, 0),)).fetchall()
    assert [r[0] for r in only_a] == ["a1", "a2"]  # nothing from meeting B leaks in
    both = conn.execute("SELECT chunk_id FROM v WHERE embedding MATCH ? AND k = 5 AND meeting_id IN ('A', 'B')",
                        (vec(1, 0, 0),)).fetchall()
    assert {r[0] for r in both} == {"a1", "a2", "b1"}


def test_vec0_metadata_column_filter_and_in_place_update_and_delete(conn):
    conn.execute("CREATE VIRTUAL TABLE v USING vec0(chunk_id text primary key, embedding float[3], kind integer)")
    conn.executemany("INSERT INTO v(chunk_id, embedding, kind) VALUES (?, ?, ?)",
                     [("x", vec(1, 0, 0), 1), ("y", vec(0.9, 0, 0), 2)])
    assert conn.execute("SELECT chunk_id FROM v WHERE embedding MATCH ? AND k = 5 AND kind = 2", (vec(1, 0, 0),)).fetchall() == [("y",)]
    conn.execute("UPDATE v SET embedding = ? WHERE chunk_id = 'y'", (vec(0, 0, 1),))  # rebuild a chunk in place
    conn.execute("DELETE FROM v WHERE chunk_id = 'x'")
    assert conn.execute("SELECT COUNT(*) FROM v").fetchone()[0] == 1


def test_vec0_rejects_a_vector_of_the_wrong_dimension(conn):
    conn.execute("CREATE VIRTUAL TABLE v USING vec0(embedding float[3])")
    with pytest.raises(sqlite3.OperationalError, match="Dimension mismatch"):
        conn.execute("INSERT INTO v(embedding) VALUES (?)", (vec(1, 0),))


def test_a_database_with_vec0_tables_cannot_be_read_without_loading_the_extension(tmp_path):
    """Consequence for CON-08: the connection factory must load sqlite-vec before any query touches a vec0 table,
    including the migration that creates it, and plain sqlite3 tools cannot read those tables."""
    path = tmp_path / "vec.db"
    writer = sqlite3.connect(path)
    writer.enable_load_extension(True)
    sqlite_vec.load(writer)
    writer.execute("CREATE VIRTUAL TABLE v USING vec0(embedding float[3])")
    writer.commit()
    writer.close()
    plain = sqlite3.connect(path)
    with pytest.raises(sqlite3.OperationalError, match="no such module: vec0"):
        plain.execute("SELECT COUNT(*) FROM v")
    plain.close()
