"""CON-16 test support: meetings with a ready summary and action items, written through the real repositories.

The summary rows are inserted the way ``SummaryService._persist_success`` writes them (ready summary, ``open``
items, ``summary_generated``), without a model, so the action-item tests need no reasoning adapter.
"""
from dataclasses import dataclass, field

from server import registry
from server.audit import emit
from server.ids import new_id
from server.repositories import audit_events, summaries
from server.timeutil import utc_now


@dataclass
class Seeded:
    meeting_id: str
    summary_id: str
    people: dict = field(default_factory=dict)   # display name -> participant_id
    items: list = field(default_factory=list)    # action_item_id in the model's order


def add_summary(tx, meeting_id: str, items, people: dict) -> tuple[str, list[str]]:
    """A ready summary with ``items`` = [(text, owner name or None)], all ``open``."""
    summary_id = new_id()
    summaries.insert_pending(tx.conn, summary_id=summary_id, meeting_id=meeting_id, model_identifier="fake")
    summaries.finish(tx.conn, summary_id, status="ready", generated_at=utc_now(), summary_text="The team talked.")
    ids = []
    for text, owner in items:
        item = summaries.ActionItem(new_id(), summary_id, meeting_id, text, people.get(owner), "open")
        summaries.insert_action_item(tx.conn, item)
        ids.append(item.action_item_id)
    emit(tx, "summary_generated", "summary", {
        "summary_id": summary_id, "input_as_of_seq": audit_events.max_seq(tx.conn), "model_identifier": "fake",
        "action_item_count": len(ids), "attempts": 1, "duration_ms": 1, "drain_timed_out": False},
        meeting_id=meeting_id)
    return summary_id, ids


def seed_meeting(db, *, title="Planning", people=("Priya", "Sam"), items=(("Write the notes", "Sam"),),
                 ended=True) -> Seeded:
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, title)
        seeded = Seeded(meeting.meeting_id, "")
        for name in people:
            registration = registry.register_device(tx, meeting.meeting_id, new_id(), name)
            seeded.people[name] = registration.participants[0].participant_id
        seeded.summary_id, seeded.items = add_summary(tx, meeting.meeting_id, items, seeded.people)
        if ended:
            registry.end_meeting(tx, meeting.meeting_id)
    return seeded


def item_row(db, action_item_id: str) -> dict:
    return dict(db.conn.execute("SELECT * FROM ActionItem WHERE action_item_id = ?", (action_item_id,)).fetchone())


def events(db, event_type: str) -> list:
    return audit_events.list_events(db.conn, event_type=event_type)
