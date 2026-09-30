"""CON-18 test support: ended meetings at chosen creation times, with summaries and edits at chosen times."""
from server import registry
from server.action_items import service as action_items
from server.ids import new_id
from server.repositories import summaries
from server.timeutil import utc_now
from tests.support.action_items import Seeded, add_summary


def meeting_at(db, created_at: str, *, title="Planning", people=("Priya", "Sam"), items=(("Write the notes", "Sam"),),
               summary="ready", ended=True) -> Seeded:
    """``summary``: ``ready`` (with ``items``), ``failed`` (one failed attempt only) or ``none``."""
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, title, now=created_at)
        seeded = Seeded(meeting.meeting_id, "")
        for name in people:
            seeded.people[name] = registry.register_device(tx, meeting.meeting_id, new_id(), name).participants[0].participant_id
        if summary == "ready":
            seeded.summary_id, seeded.items = add_summary(tx, meeting.meeting_id, items, seeded.people)
        elif summary == "failed":
            fail_summary(tx, meeting.meeting_id)
        if ended:
            registry.end_meeting(tx, meeting.meeting_id)
    return seeded


def fail_summary(tx, meeting_id: str, code: str = "summary_invalid_output") -> str:
    """A failed attempt with its ``summary_failed`` event, the way the summary service records one."""
    from server.audit import emit
    summary_id = new_id()
    summaries.insert_pending(tx.conn, summary_id=summary_id, meeting_id=meeting_id, model_identifier="fake")
    summaries.finish(tx.conn, summary_id, status="failed", generated_at=utc_now(), error_message="bad output")
    emit(tx, "summary_failed", "summary", {"summary_id": summary_id, "error_code": code, "attempts": 2,
                                           "duration_ms": 1, "drain_timed_out": False}, meeting_id=meeting_id)
    return summary_id


def edit_at(db, action_item_id: str, at: str, **changes) -> None:
    """Edit an item, then move its ``action_item_updated`` event to ``at`` (the event time is what counts)."""
    with db.transaction() as tx:
        action_items.update_action_item(tx, action_item_id, changes)
        seq = tx.conn.execute("SELECT MAX(seq) FROM AuditEvent WHERE event_type = 'action_item_updated'").fetchone()[0]
        tx.conn.execute("UPDATE AuditEvent SET timestamp = ? WHERE seq = ?", (at, seq))
