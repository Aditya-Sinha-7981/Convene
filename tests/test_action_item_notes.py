"""CON-16: notes attached to an existing action item from a later meeting, and ADR-27 deletion of notes."""
import pytest
from aiohttp import ClientSession

from server import registry
from server.action_items import service
from server.errors import ActionItemNotFoundError, MeetingNotFoundError, StorageError, ValidationError
from server.ids import new_id
from tests.support.action_items import events, item_row, seed_meeting


def note(db, item_id, source, text="Draft is with Priya.", status=None):
    with db.transaction() as tx:
        return service.add_action_item_note(tx, item_id, source, text, status)


def count(db, sql, *params):
    return db.conn.execute(sql, params).fetchone()[0]


def test_a_note_from_a_later_meeting_attaches_to_the_original_item(db):
    first = seed_meeting(db, title="Planning")
    later = seed_meeting(db, title="Weekly sync", items=[])
    [item_id] = first.items
    before = item_row(db, item_id)
    stored = note(db, item_id, later.meeting_id, "  The confidential draft is with Priya.  ")
    assert stored.text == "The confidential draft is with Priya." and stored.source_meeting_id == later.meeting_id
    assert count(db, "SELECT COUNT(*) FROM ActionItem") == 1  # never a duplicate item
    assert item_row(db, item_id) == before  # no status change requested
    [event] = events(db, "action_item_note_added")
    assert event.meeting_id == first.meeting_id  # the item's originating meeting
    assert event.payload == {"note_id": stored.note_id, "action_item_id": item_id, "source_meeting_id": later.meeting_id}
    assert events(db, "action_item_updated") == []
    raw = db.conn.execute("SELECT payload FROM AuditEvent WHERE event_type = 'action_item_note_added'").fetchone()[0]
    assert "confidential" not in raw


def test_a_note_can_change_the_status_in_the_same_transaction(db):
    first = seed_meeting(db)
    later = seed_meeting(db, title="Later", items=[])
    [item_id] = first.items
    note(db, item_id, later.meeting_id, "Done last week.", status="done")
    assert item_row(db, item_id)["status"] == "done"
    [added] = events(db, "action_item_note_added")
    [updated] = events(db, "action_item_updated")
    assert updated.payload["via"] == "note" and updated.payload["from"] == {"status": "open"}
    assert updated.payload["to"] == {"status": "done"} and updated.seq > added.seq
    note(db, item_id, later.meeting_id, "Still done.", status="done")  # same status: a note, no edit
    assert len(events(db, "action_item_updated")) == 1 and len(events(db, "action_item_note_added")) == 2


def test_a_failed_status_change_rolls_back_the_note(db, monkeypatch):
    first = seed_meeting(db)
    [item_id] = first.items
    real = service.emit

    def emit(tx, event_type, *args, **kwargs):
        if event_type == "action_item_updated":
            raise StorageError("disk full")
        return real(tx, event_type, *args, **kwargs)
    monkeypatch.setattr(service, "emit", emit)
    with pytest.raises(StorageError):
        note(db, item_id, first.meeting_id, "Done.", status="done")
    assert count(db, "SELECT COUNT(*) FROM ActionItemNote") == 0 and events(db, "action_item_note_added") == []
    assert item_row(db, item_id)["status"] == "open"


@pytest.mark.parametrize("text,status,error", [
    ("", None, ValidationError), ("   ", None, ValidationError), (None, None, ValidationError),
    ("x" * 2001, None, ValidationError), ("ok", "archived", ValidationError),
])
def test_invalid_notes_are_rejected_with_nothing_written(db, text, status, error):
    first = seed_meeting(db)
    with pytest.raises(error):
        note(db, first.items[0], first.meeting_id, text, status)
    assert count(db, "SELECT COUNT(*) FROM ActionItemNote") == 0 and events(db, "action_item_note_added") == []


def test_the_longest_note_is_accepted(db):
    first = seed_meeting(db)
    assert len(note(db, first.items[0], first.meeting_id, "x" * 2000).text) == 2000


def test_unknown_source_meeting_and_item(db):
    first = seed_meeting(db)
    with pytest.raises(MeetingNotFoundError):
        note(db, first.items[0], new_id())
    with pytest.raises(ValidationError, match="source_meeting_id"):
        note(db, first.items[0], "not-a-uuid")
    with pytest.raises(ActionItemNotFoundError):
        note(db, new_id(), first.meeting_id)


# --- ADR-27 deletion ----------------------------------------------------------------------------------------------


def erase(db, meeting_id):
    with db.transaction() as tx:
        registry.erase_meeting(tx, meeting_id)


def test_deleting_the_items_meeting_removes_its_items_and_every_note_on_them(db):
    first = seed_meeting(db)
    later = seed_meeting(db, title="Later", items=[("Book a room", "Priya")])
    note(db, first.items[0], first.meeting_id, "Own meeting note.")
    note(db, first.items[0], later.meeting_id, "From the later meeting.", status="done")
    kept = note(db, later.items[0], later.meeting_id, "Unrelated.")
    erase(db, first.meeting_id)
    assert count(db, "SELECT COUNT(*) FROM ActionItem WHERE meeting_id = ?", first.meeting_id) == 0
    assert [row[0] for row in db.conn.execute("SELECT note_id FROM ActionItemNote")] == [kept.note_id]
    assert count(db, "SELECT COUNT(*) FROM AuditEvent WHERE meeting_id = ?", first.meeting_id) == 0
    assert count(db, "SELECT COUNT(*) FROM ActionItem WHERE meeting_id = ?", later.meeting_id) == 1
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_deleting_the_source_meeting_removes_the_notes_it_added_but_keeps_the_status(db):
    first = seed_meeting(db)
    later = seed_meeting(db, title="Later", items=[])
    own = note(db, first.items[0], first.meeting_id, "Own meeting note.")
    note(db, first.items[0], later.meeting_id, "Done, said in the later meeting.", status="done")
    erase(db, later.meeting_id)
    assert [row[0] for row in db.conn.execute("SELECT note_id FROM ActionItemNote")] == [own.note_id]
    assert [e.payload["note_id"] for e in events(db, "action_item_note_added")] == [own.note_id]
    [updated] = events(db, "action_item_updated")  # the change to the surviving item stays recorded
    assert updated.meeting_id == first.meeting_id and item_row(db, first.items[0])["status"] == "done"
    assert db.conn.execute("PRAGMA foreign_key_check").fetchall() == []


# --- HTTP -----------------------------------------------------------------------------------------------------------


@pytest.fixture
async def http():
    async with ClientSession() as session:
        yield session


@pytest.mark.asyncio
async def test_note_routes(server, http):
    db = server.runtime.db
    first = seed_meeting(db)
    later = seed_meeting(db, title="Weekly sync", items=[])
    url = server.base_url + f"/api/action-items/{first.items[0]}"
    body = {"source_meeting_id": later.meeting_id, "text": "Draft is with Priya.", "status": "done"}
    async with http.post(url + "/notes", json=body) as response:
        assert response.status == 201
        created = await response.json()
    assert created["note"]["source_meeting_title"] == "Weekly sync" and created["note"]["text"] == "Draft is with Priya."
    assert created["action_item"]["status"] == "done" and created["action_item"]["note_count"] == 1
    async with http.get(url) as response:
        detail = await response.json()
    assert [n["note_id"] for n in detail["notes"]] == [created["note"]["note_id"]]
    assert detail["action_item"]["last_changed_by"] == "manual"

    for bad, status, code in [({"source_meeting_id": new_id(), "text": "x"}, 404, "meeting_not_found"),
                              ({"source_meeting_id": later.meeting_id, "text": ""}, 400, "invalid_request"),
                              ({"source_meeting_id": later.meeting_id, "text": "x", "owner": "Sam"}, 400, "invalid_request"),
                              ({"text": "x"}, 400, "invalid_request")]:
        async with http.post(url + "/notes", json=bad) as response:
            assert response.status == status and (await response.json())["error"]["code"] == code, bad
    async with http.post(server.base_url + f"/api/action-items/{new_id()}/notes",
                         json={"source_meeting_id": later.meeting_id, "text": "x"}) as response:
        assert response.status == 404 and (await response.json())["error"]["code"] == "action_item_not_found"
    assert db.conn.execute("SELECT COUNT(*) FROM ActionItemNote").fetchone()[0] == 1
