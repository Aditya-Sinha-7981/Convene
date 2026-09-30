"""CON-16: migration 0009, the frozen summary pipeline, the edit service, its audit record, and export staleness."""
import asyncio
import io
import shutil
import sqlite3

import pytest
from aiohttp import ClientSession
from docx import Document

from server.action_items import service
from server.action_items.views import item_view
from server.attribution import AttributionService
from server.config import AttributionConfig, SummaryConfig
from server.db import MIGRATIONS_DIR, connect, migrate
from server.errors import ActionItemNotFoundError, StorageError, ValidationError
from server.ids import new_id
from server.rag.reasoning import FakeReasoningAdapter
from server.summary import SummaryService
from server.summary.views import summary_payload
from tests.support.action_items import events, item_row, seed_meeting
from tests.support.summary import create_meeting, say, summary_json

# --- migration ----------------------------------------------------------------------------------------------------


def test_migration_keeps_every_existing_item_and_widens_the_status(tmp_path):
    old = tmp_path / "old"
    old.mkdir()
    for path in MIGRATIONS_DIR.glob("000[1-8]_*.sql"):
        shutil.copy(path, old)
    conn = connect(tmp_path / "c.db")
    assert migrate(conn, old) == 8
    meeting_id, summary_id = new_id(), new_id()
    conn.execute("INSERT INTO Meeting (meeting_id, title, status, created_at) VALUES (?, 'm', 'ended', "
                 "'2026-09-21T10:00:00.000Z')", (meeting_id,))
    conn.execute("INSERT INTO Summary VALUES (?, ?, 'ready', 'text', NULL, 'fake', '2026-09-21T10:05:00.000Z')",
                 (summary_id, meeting_id))
    before = [(new_id(), summary_id, meeting_id, f"item {n}", None, status) for n, status in
              enumerate(("open", "done", "open"))]
    conn.executemany("INSERT INTO ActionItem VALUES (?, ?, ?, ?, ?, ?)", before)

    for path in MIGRATIONS_DIR.glob("0009_*.sql"):  # stop at 0009 so later migrations don't change this test
        shutil.copy(path, old)
    assert migrate(conn, old) == 9
    after = conn.execute("SELECT action_item_id, summary_id, meeting_id, text, owner_participant_id, status, due_date "
                         "FROM ActionItem ORDER BY rowid").fetchall()
    assert [tuple(row) for row in after] == [(*row, None) for row in before]  # same rows, same order
    indexes = {row[1]: [c[2] for c in conn.execute(f"PRAGMA index_info({row[1]})")]
               for row in conn.execute("PRAGMA index_list(ActionItem)")}
    assert indexes["idx_action_item_summary"] == ["summary_id"]
    assert indexes["idx_action_item_status_due"] == ["status", "due_date"]
    assert "ActionItem_old" not in {r[0] for r in conn.execute("SELECT name FROM sqlite_master")}
    conn.execute("UPDATE ActionItem SET status = 'cancelled', due_date = '2026-10-01' WHERE action_item_id = ?",
                 (before[0][0],))
    for bad in ({"status": "archived"}, {"due_date": "2026-02-30"}, {"due_date": "2026-9-1"},
                {"due_date": "2026-09-01T00:00"}):
        column, value = next(iter(bad.items()))
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(f"UPDATE ActionItem SET {column} = ? WHERE action_item_id = ?", (value, before[1][0]))
    assert migrate(conn, old) == 9  # a second run is a no-op
    assert conn.execute("SELECT COUNT(*) FROM ActionItem").fetchone()[0] == 3
    conn.close()


# --- the summary pipeline is unchanged ------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_a_fresh_summary_still_inserts_open_items_without_a_due_date(db):
    attribution = AttributionService(db, AttributionConfig())
    seeded = await create_meeting(db, ["Priya", "Sam"])
    await say(attribution, seeded, "Sam", 5, "I'll write the release notes.")
    adapter = FakeReasoningAdapter(lambda messages: summary_json(items=[("Write the release notes", "Sam"),
                                                                        ("Book a room", None)]))
    summary = SummaryService(db, SummaryConfig(drain_timeout_s=1.0, generation_timeout_s=5.0), reasoning=adapter,
                             attribution=attribution)
    await summary.summarize(seeded.meeting_id)
    while summary._tasks:
        await asyncio.gather(*list(summary._tasks))
    rows = [dict(r) for r in db.conn.execute("SELECT status, due_date, owner_participant_id FROM ActionItem ORDER BY rowid")]
    assert rows == [{"status": "open", "due_date": None, "owner_participant_id": seeded.participants["Sam"]},
                    {"status": "open", "due_date": None, "owner_participant_id": None}]
    items = summary_payload(db.conn, seeded.meeting_id)["action_items"]
    assert [(i["text"], i["owner_display_name"], i["last_changed_by"], i["note_count"]) for i in items] == [
        ("Write the release notes", "Sam", "summary", 0), ("Book a room", None, "summary", 0)]


# --- edit service ---------------------------------------------------------------------------------------------------


def edit(db, item_id, changes):
    with db.transaction() as tx:
        return service.update_action_item(tx, item_id, changes)


@pytest.mark.parametrize("changes", [
    {"status": "done"}, {"status": "cancelled"}, {"due_date": "2026-10-01"}, {"owner_participant_id": None},
    {"owner_participant_id": "PRIYA", "due_date": "2026-10-01", "status": "done"},
])
def test_each_field_alone_and_together_is_applied_with_one_audit_event(db, changes):
    seeded = seed_meeting(db)
    [item_id] = seeded.items
    changes = {k: seeded.people["Priya"] if v == "PRIYA" else v for k, v in changes.items()}
    before = item_row(db, item_id)
    assert edit(db, item_id, changes) is True
    after = item_row(db, item_id)
    assert after == {**before, **changes}
    [event] = events(db, "action_item_updated")
    assert event.meeting_id == seeded.meeting_id and event.component == "api"
    assert event.payload == {"action_item_id": item_id, "summary_id": seeded.summary_id, "via": "edit",
                             "from": {k: before[k] for k in changes}, "to": changes}
    view = item_view(db.conn, item_id)
    assert view["last_changed_by"] == "manual" and view["last_changed_at"] == event.timestamp


def test_only_changed_fields_are_recorded_and_no_text_reaches_the_audit_payload(db):
    seeded = seed_meeting(db, items=[("Send the secret budget to Sam", "Sam")])
    [item_id] = seeded.items
    edit(db, item_id, {"owner_participant_id": seeded.people["Sam"], "status": "done"})  # owner unchanged
    [event] = events(db, "action_item_updated")
    assert event.payload["from"] == {"status": "open"} and event.payload["to"] == {"status": "done"}
    raw = db.conn.execute("SELECT payload FROM AuditEvent WHERE event_type = 'action_item_updated'").fetchone()[0]
    assert "secret" not in raw and "budget" not in raw


def test_a_no_op_edit_writes_and_emits_nothing(db):
    seeded = seed_meeting(db)
    [item_id] = seeded.items
    before, seq = item_row(db, item_id), db.conn.execute("SELECT MAX(seq) FROM AuditEvent").fetchone()[0]
    assert edit(db, item_id, {"status": "open", "owner_participant_id": seeded.people["Sam"], "due_date": None}) is False
    assert item_row(db, item_id) == before
    assert db.conn.execute("SELECT MAX(seq) FROM AuditEvent").fetchone()[0] == seq


@pytest.mark.parametrize("changes,field", [
    ({}, "at least one"), ({"text": "rewritten"}, "text"), ({"status": "archived"}, "status"),
    ({"status": None}, "status"), ({"due_date": "2026-02-30"}, "due_date"), ({"due_date": "tomorrow"}, "due_date"),
    ({"due_date": 20261001}, "due_date"), ({"owner_participant_id": "not-a-uuid"}, "owner_participant_id"),
    ({"owner_participant_id": "OTHER"}, "owner_participant_id"), ({"owner_participant_id": "UNKNOWN"}, "owner_participant_id"),
])
def test_invalid_edits_are_rejected_with_nothing_written(db, changes, field):
    seeded = seed_meeting(db)
    other = seed_meeting(db, title="Other")
    [item_id] = seeded.items
    swap = {"OTHER": other.people["Priya"], "UNKNOWN": new_id()}
    changes = {k: swap.get(v, v) if isinstance(v, str) else v for k, v in changes.items()}
    before = item_row(db, item_id)
    with pytest.raises(ValidationError, match=field):
        edit(db, item_id, changes)
    assert item_row(db, item_id) == before and events(db, "action_item_updated") == []


def test_unknown_item_is_not_found(db):
    with pytest.raises(ActionItemNotFoundError):
        edit(db, new_id(), {"status": "done"})


def test_row_and_audit_event_are_atomic(db, monkeypatch):
    seeded = seed_meeting(db)
    [item_id] = seeded.items
    before = item_row(db, item_id)

    def broken_emit(*args, **kwargs):
        raise StorageError("disk full")
    monkeypatch.setattr(service, "emit", broken_emit)
    with pytest.raises(StorageError):
        edit(db, item_id, {"status": "done", "due_date": "2026-10-01"})
    assert item_row(db, item_id) == before and events(db, "action_item_updated") == []


def test_items_of_a_superseded_summary_stay_editable(db):
    from tests.support.action_items import add_summary
    seeded = seed_meeting(db)
    with db.transaction() as tx:
        add_summary(tx, seeded.meeting_id, [("Write the notes", "Sam")], seeded.people)
    assert edit(db, seeded.items[0], {"status": "done"}) is True
    assert [i["action_item_id"] for i in summary_payload(db.conn, seeded.meeting_id)["action_items"]] != seeded.items


# --- HTTP and export staleness ----------------------------------------------------------------------------------------


@pytest.fixture
async def http():
    async with ClientSession() as session:
        yield session


def status_cells(content: bytes) -> list[str]:
    table = Document(io.BytesIO(content)).tables[0]
    return [row.cells[2].text for row in table.rows[1:]]


@pytest.mark.asyncio
async def test_an_edit_makes_the_docx_stale_but_not_the_summary(server, http):
    seeded = seed_meeting(server.runtime.db)
    [item_id] = seeded.items
    base = server.base_url + f"/api/meetings/{seeded.meeting_id}"
    async with http.get(base + "/export") as response:
        assert response.status == 200 and status_cells(await response.read()) == ["open"]
    async with http.get(base + "/export/status") as response:
        assert (await response.json())["stale"] is False

    async with http.patch(server.base_url + f"/api/action-items/{item_id}", json={"status": "done"}) as response:
        assert response.status == 200
        body = await response.json()
    assert body["changed"] is True and body["action_item"]["status"] == "done"
    async with http.get(base + "/export/status") as response:
        assert (await response.json())["stale"] is True
    async with http.get(base + "/summary") as response:
        summary = await response.json()
    assert summary["stale"] is False and summary["action_items"][0]["status"] == "done"
    async with http.get(base + "/export") as response:
        assert status_cells(await response.read()) == ["done"]  # re-rendered, never served out of date
    async with http.get(base + "/export/status") as response:
        assert (await response.json())["stale"] is False
    # A no-op edit does not make the fresh file stale again.
    async with http.patch(server.base_url + f"/api/action-items/{item_id}", json={"status": "done"}) as response:
        assert (await response.json())["changed"] is False
    async with http.get(base + "/export/status") as response:
        assert (await response.json())["stale"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize("body,status,code", [
    ({"status": "archived"}, 400, "invalid_request"), ({"title": "x"}, 400, "invalid_request"),
    ({}, 400, "invalid_request"), ({"due_date": "2026-13-01"}, 400, "invalid_request"),
])
async def test_bad_patch_bodies(server, http, body, status, code):
    seeded = seed_meeting(server.runtime.db)
    async with http.patch(server.base_url + f"/api/action-items/{seeded.items[0]}", json=body) as response:
        assert response.status == status and (await response.json())["error"]["code"] == code


@pytest.mark.asyncio
async def test_unknown_and_malformed_item_ids(server, http):
    async with http.patch(server.base_url + f"/api/action-items/{new_id()}", json={"status": "done"}) as response:
        assert response.status == 404 and (await response.json())["error"]["code"] == "action_item_not_found"
    async with http.get(server.base_url + f"/api/action-items/{new_id()}") as response:
        assert response.status == 404 and (await response.json())["error"]["code"] == "action_item_not_found"
    async with http.patch(server.base_url + "/api/action-items/nope", json={"status": "done"}) as response:
        assert response.status == 400 and (await response.json())["error"]["code"] == "invalid_request"
