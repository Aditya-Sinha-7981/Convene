"""The single audit write path: validation, sequencing, post-commit delivery, atomicity, concurrency."""
import asyncio
import re
import sqlite3
import threading
import time
from pathlib import Path

import pytest

from server import audit
from server.audit_catalog import CATALOG, CONNECTION_TO_AUDIT
from server.db import Database
from server.errors import (ConstraintError, InvalidPayloadError, NotFoundError, StorageError,
                           UnknownEventTypeError, ValidationError)
from server.repositories import audit_events, connections, devices, meetings, participants
from tests.support.storage import device, meeting, participant
from tests.test_api_contract_docs import AUDIT as DOCUMENTED_AUDIT

ROOT = Path(__file__).resolve().parents[1]
TS = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")


def payload_for(event_type):
    return {key: None for key in CATALOG[event_type].payload_keys}


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


@pytest.fixture
def m_and_d(db):
    m = meeting()
    d = device(m.meeting_id)
    with db.transaction() as tx:
        meetings.create(tx.conn, m)
        devices.create(tx.conn, d)
        participants.create(tx.conn, participant(m.meeting_id, d.device_id))
    return m, d


# --- catalog --------------------------------------------------------------------------------


def test_code_catalog_equals_the_documented_catalog():
    assert set(CATALOG) == set(DOCUMENTED_AUDIT)
    for event_type, spec in CATALOG.items():
        assert spec.payload_keys == DOCUMENTED_AUDIT[event_type], event_type


def test_catalog_components_match_the_documented_components():
    import re as _re
    from tests.test_api_contract_docs import DATA_MODEL, heading_section, tables, ticks
    section = heading_section(DATA_MODEL, lambda lvl, title: title == "Audit event catalog")
    rows = next(rows for header, rows in tables(section) if header[:1] == ["event_type"])
    documented = {ticks(r[0])[0]: ticks(r[1])[0] for r in rows}
    assert {k: v.component for k, v in CATALOG.items()} == documented
    assert _re  # keep the import local and obvious


def test_connection_types_map_onto_catalogued_audit_types():
    assert set(CONNECTION_TO_AUDIT.values()) <= set(CATALOG)
    assert len(set(CONNECTION_TO_AUDIT.values())) == 4


# --- validation: rejected events write nothing ---------------------------------------------


def test_unknown_event_type_is_rejected_and_nothing_is_written(db, m_and_d):
    m, _ = m_and_d
    with pytest.raises(UnknownEventTypeError):
        with db.transaction() as tx:
            audit.emit(tx, "device_teleported", "transport", {}, meeting_id=m.meeting_id)
    assert count(db.conn, "AuditEvent") == 0


@pytest.mark.parametrize("event_type,component,payload,meeting,message", [
    ("meeting_created", "transport", {"title": "x"}, True, "component"),
    ("meeting_created", "api", {"title": "x"}, False, "requires a meeting_id"),
    ("meeting_created", "api", {}, True, "missing"),
    ("meeting_created", "api", {"title": "x", "text": "the whole transcript"}, True, "unexpected"),
    ("meeting_created", "api", ["title"], True, "JSON object"),
    ("meeting_created", "api", {"title": object()}, True, "JSON-serializable"),
    ("meeting_created", "api", {"title": float("nan")}, True, "JSON-serializable"),
])
def test_invalid_events_are_rejected_before_any_write(db, m_and_d, event_type, component, payload, meeting, message):
    m, _ = m_and_d
    with pytest.raises(InvalidPayloadError, match=message):
        with db.transaction() as tx:
            audit.emit(tx, event_type, component, payload, meeting_id=m.meeting_id if meeting else None)
    assert count(db.conn, "AuditEvent") == 0


def test_events_outside_a_meeting_may_omit_it_and_others_may_not(db):
    with db.transaction() as tx:
        event = audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0})
    assert event.meeting_id is None
    optional = {t for t, s in CATALOG.items() if not s.meeting_required}
    assert optional == {"server_started", "model_load", "model_error", "signaling_error", "qa_query"}


def test_unknown_meeting_is_a_foreign_key_error(db):
    with pytest.raises(ConstraintError):
        with db.transaction() as tx:
            audit.emit(tx, "meeting_created", "api", {"title": "x"}, meeting_id="0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8")


# --- the row, seq, timestamps ---------------------------------------------------------------


def test_emit_writes_the_documented_row(db, m_and_d):
    m, _ = m_and_d
    with db.transaction() as tx:
        event = audit.emit(tx, "meeting_created", "api", {"title": "Sprint planning"}, meeting_id=m.meeting_id)
    stored = audit_events.get(db.conn, event.event_id)
    assert stored == event
    assert (stored.seq, stored.component, stored.payload) == (1, "api", {"title": "Sprint planning"})
    assert stored.meeting_id == m.meeting_id and TS.match(stored.timestamp)
    raw = db.conn.execute("SELECT payload FROM AuditEvent").fetchone()[0]
    assert raw == '{"title":"Sprint planning"}'  # a JSON object, compact and key-sorted


def test_seq_is_strictly_increasing_across_transactions_and_survives_reopen(tmp_path):
    path = tmp_path / "seq.db"
    first = Database.open(path)
    seen = []
    for _ in range(3):
        with first.transaction() as tx:
            seen.append(audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0}).seq)
    first.close()
    second = Database.open(path)
    with second.transaction() as tx:
        seen.append(audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0}).seq)
    assert seen == [1, 2, 3, 4]
    assert audit_events.max_seq(second.conn) == 4
    second.close()


def test_rolled_back_transaction_does_not_burn_a_seq(db):
    with pytest.raises(RuntimeError):
        with db.transaction() as tx:
            audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0})
            raise RuntimeError("boom")
    with db.transaction() as tx:
        assert audit.emit(tx, "server_started", "api", {"reconciled_devices": 1, "reconciled_meetings": 1}).seq == 1


def test_list_events_filters_and_orders_by_seq(db, m_and_d):
    m, _ = m_and_d
    with db.transaction() as tx:
        audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0})
        audit.emit(tx, "meeting_created", "api", {"title": "a"}, meeting_id=m.meeting_id)
        audit.emit(tx, "meeting_started", "transport", {"first_device_id": "d"}, meeting_id=m.meeting_id)
    assert [e.seq for e in audit_events.list_events(db.conn)] == [1, 2, 3]
    assert [e.event_type for e in audit_events.list_events(db.conn, meeting_id=m.meeting_id)] == [
        "meeting_created", "meeting_started"]
    assert [e.seq for e in audit_events.list_events(db.conn, after_seq=1)] == [2, 3]
    assert [e.seq for e in audit_events.list_events(db.conn, event_type="meeting_started")] == [3]


# --- subscribers ----------------------------------------------------------------------------


def test_subscribers_run_after_commit_and_see_committed_data(tmp_path):
    path = tmp_path / "sub.db"
    database = Database.open(path)
    observed = []

    def subscriber(event):
        # A different connection can only see the row if the transaction has committed.
        other = sqlite3.connect(path)
        observed.append((event.seq, other.execute("SELECT COUNT(*) FROM AuditEvent").fetchone()[0]))
        other.close()

    database.subscribe(subscriber)
    with database.transaction() as tx:
        audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0})
        audit.emit(tx, "server_started", "api", {"reconciled_devices": 1, "reconciled_meetings": 0})
        assert observed == []  # nothing delivered while the transaction is open
    assert observed == [(1, 2), (2, 2)]
    database.close()


def test_subscribers_are_not_called_when_the_transaction_rolls_back(db):
    seen = []
    db.subscribe(seen.append)
    with pytest.raises(RuntimeError):
        with db.transaction() as tx:
            audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0})
            raise RuntimeError("boom")
    assert seen == []


def test_a_failing_subscriber_does_not_fail_the_write_or_other_subscribers(db, caplog):
    calls = []

    def broken(event):
        raise RuntimeError("subscriber bug")

    db.subscribe(broken)
    db.subscribe(lambda e: calls.append(e.seq))
    with db.transaction() as tx:
        audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0})
    assert calls == [1]
    assert count(db.conn, "AuditEvent") == 1
    assert "audit subscriber failed" in caplog.text


def test_unsubscribe(db):
    seen = []
    db.subscribe(seen.append)
    db.unsubscribe(seen.append)
    with db.transaction() as tx:
        audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0})
    assert seen == []


# --- ConnectionEvent + AuditEvent atomicity -------------------------------------------------


def test_record_connection_event_writes_both_rows_linked_by_event_id(db, m_and_d):
    _, d = m_and_d
    seen = []
    db.subscribe(seen.append)
    with db.transaction() as tx:
        connection, event = audit.record_connection_event(tx, device_id=d.device_id, event_type="connected",
                                                          payload={"reconnect_count": 0})
    assert connections.get(db.conn, connection.event_id) == connection
    assert audit_events.get(db.conn, event.event_id) == event
    assert connection.event_id == event.event_id
    assert (event.event_type, event.component, event.meeting_id) == ("device_connected", "transport", d.meeting_id)
    assert event.payload == {"device_id": d.device_id, "reconnect_count": 0}
    assert connection.timestamp == event.timestamp
    assert seen == [event]


@pytest.mark.parametrize("connection_type", sorted(CONNECTION_TO_AUDIT))
def test_each_connection_type_projects_its_audit_type(db, m_and_d, connection_type):
    _, d = m_and_d
    audit_type = CONNECTION_TO_AUDIT[connection_type]
    extra = {key: None for key in CATALOG[audit_type].payload_keys - {"device_id"}}
    with db.transaction() as tx:
        _, event = audit.record_connection_event(tx, device_id=d.device_id, event_type=connection_type, payload=extra)
    assert event.event_type == audit_type


def test_a_failed_second_insert_persists_neither_row(db, m_and_d, monkeypatch):
    _, d = m_and_d
    seen = []
    db.subscribe(seen.append)

    def fail(tx, event):
        raise ConstraintError("injected failure on the ConnectionEvent insert")

    monkeypatch.setattr(audit, "_insert_connection_event", fail)
    with pytest.raises(ConstraintError, match="injected"):
        with db.transaction() as tx:
            audit.record_connection_event(tx, device_id=d.device_id, event_type="connected",
                                          payload={"reconnect_count": 0})
    assert count(db.conn, "AuditEvent") == 0
    assert count(db.conn, "ConnectionEvent") == 0
    assert seen == []  # and no subscriber heard about it


def test_a_rejected_audit_event_leaves_no_connection_event(db, m_and_d):
    _, d = m_and_d
    with pytest.raises(InvalidPayloadError):
        with db.transaction() as tx:
            audit.record_connection_event(tx, device_id=d.device_id, event_type="disconnected", payload={})
    assert count(db.conn, "AuditEvent") == 0 and count(db.conn, "ConnectionEvent") == 0


def test_record_connection_event_validates_its_inputs(db, m_and_d):
    _, d = m_and_d
    with pytest.raises(ValidationError):
        with db.transaction() as tx:
            audit.record_connection_event(tx, device_id=d.device_id, event_type="flapping")
    with pytest.raises(NotFoundError):
        with db.transaction() as tx:
            audit.record_connection_event(tx, device_id="0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
                                          event_type="connected", payload={"reconnect_count": 0})


def test_audit_and_connection_inserts_exist_only_in_the_audit_module():
    """ADR-13: one write path. No other module may INSERT into AuditEvent or ConnectionEvent."""
    pattern = re.compile(r"INSERT\s+(?:OR\s+\w+\s+)?INTO\s+(AuditEvent|ConnectionEvent)\b", re.I)
    offenders = []
    for path in (ROOT / "server").rglob("*"):
        if path.suffix in (".py", ".sql") and "migrations" not in path.parts and path.name != "audit.py":
            if pattern.search(path.read_text()):
                offenders.append(str(path.relative_to(ROOT)))
    assert offenders == []
    assert len(pattern.findall((ROOT / "server" / "audit.py").read_text())) == 2  # the two known inserts


# --- concurrency and the event loop ---------------------------------------------------------


async def test_concurrent_async_emits_get_unique_contiguous_seq_and_ordered_delivery(db):
    delivered = []
    db.subscribe(lambda e: delivered.append(e.seq))

    def one(tx):
        return audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0}).seq

    seqs = await asyncio.gather(*(db.run(one) for _ in range(150)))
    await asyncio.sleep(0)
    assert sorted(seqs) == list(range(1, 151))
    assert delivered == sorted(delivered) == list(range(1, 151))  # commit order == seq order


async def test_slow_database_work_does_not_block_the_event_loop(db):
    ticks = 0

    async def heartbeat():
        nonlocal ticks
        while True:
            await asyncio.sleep(0.005)
            ticks += 1

    def slow_write(tx):
        time.sleep(0.05)  # a slow disk / long write, on the worker thread
        return audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0}).seq

    beat = asyncio.create_task(heartbeat())
    started = time.monotonic()
    await asyncio.gather(*(db.run(slow_write) for _ in range(10)))
    elapsed = time.monotonic() - started
    beat.cancel()
    assert elapsed >= 0.45  # the ten writes really were serialized...
    assert ticks >= elapsed / 0.005 * 0.4  # ...while the loop kept running


async def test_one_failing_write_does_not_stop_another(db, m_and_d):
    m, _ = m_and_d

    def bad(tx):
        audit.emit(tx, "meeting_created", "api", {"title": "will be rolled back"}, meeting_id=m.meeting_id)
        raise RuntimeError("device A's write failed")

    def good(tx):
        return audit.emit(tx, "meeting_created", "api", {"title": "device B"}, meeting_id=m.meeting_id).seq

    results = await asyncio.gather(db.run(bad), db.run(good), db.run(good), return_exceptions=True)
    assert isinstance(results[0], RuntimeError)
    assert results[1:] == [1, 2]  # the failed write's seq was not consumed
    assert [e.payload["title"] for e in audit_events.list_events(db.conn)] == ["device B", "device B"]


async def test_run_raises_typed_errors_and_rolls_back(db):
    def bad(tx):
        tx.conn.execute("INSERT INTO Meeting VALUES ('x', 't', 'nope', 'z', NULL, NULL)")

    with pytest.raises(StorageError):
        await db.run(bad)
    assert count(db.conn, "Meeting") == 0


def test_two_databases_on_one_file_serialize_their_writers(tmp_path):
    """BEGIN IMMEDIATE + busy timeout: writers on separate connections never collide on seq."""
    path = tmp_path / "shared.db"
    a, b = Database.open(path), Database.open(path)
    errors = []

    def writer(database):
        try:
            for _ in range(40):
                with database.transaction() as tx:
                    audit.emit(tx, "server_started", "api", {"reconciled_devices": 0, "reconciled_meetings": 0})
        except Exception as exc:  # pragma: no cover - reported below
            errors.append(exc)

    threads = [threading.Thread(target=writer, args=(x,)) for x in (a, b)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert [e.seq for e in audit_events.list_events(a.conn)] == list(range(1, 81))
    a.close()
    b.close()
