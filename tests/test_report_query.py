"""CON-18: the report query service (range resolution, per-meeting content, action-item figures). No model."""
import pytest

from server import registry
from server.errors import ValidationError
from server.repositories import meetings, summaries
from server.reports.service import ReportEmptyError, build, preview
from tests.support.action_items import add_summary
from tests.support.reports import edit_at, fail_summary, meeting_at

SEPT = ("2026-09-01T00:00:00.000Z", "2026-09-30T23:59:59.999Z")


def titles(report):
    return [m["title"] for m in report["meetings"]]


def test_bounds_are_inclusive_to_the_millisecond_and_oldest_first(db):
    meeting_at(db, "2026-08-31T23:59:59.999Z", title="before")
    meeting_at(db, "2026-09-30T23:59:59.999Z", title="last ms")
    meeting_at(db, "2026-09-01T00:00:00.000Z", title="first ms")
    meeting_at(db, "2026-10-01T00:00:00.000Z", title="after")
    assert titles(build(db.conn, *SEPT, 50)) == ["first ms", "last ms"]
    assert preview(db.conn, *SEPT, 50)["meeting_count"] == 2


def test_same_meetings_as_the_history_list_for_the_same_bounds(db):
    for day in ("2026-09-02", "2026-09-10", "2026-09-29"):
        meeting_at(db, f"{day}T10:00:00.000Z", title=day)
    meeting_at(db, "2026-09-15T10:00:00.000Z", title="live", ended=False)
    listed = meetings.list_meetings(db.conn, status="ended", from_at=SEPT[0], to_at=SEPT[1])
    assert [m.meeting_id for m in reversed(listed)] == [m["meeting_id"] for m in build(db.conn, *SEPT, 50)["meetings"]]


def test_not_ended_meetings_are_excluded_and_counted(db):
    meeting_at(db, "2026-09-02T10:00:00.000Z", title="done")
    meeting_at(db, "2026-09-03T10:00:00.000Z", title="running", ended=False)
    with db.transaction() as tx:
        registry.create_meeting(tx, "never started", now="2026-09-04T10:00:00.000Z")
    body = preview(db.conn, *SEPT, 50)
    assert (body["meeting_count"], body["excluded_not_ended_count"], body["with_summary_count"]) == (1, 2, 1)
    report = build(db.conn, *SEPT, 50)
    assert titles(report) == ["done"] and report["excluded_not_ended_count"] == 2


def test_reversed_range_is_rejected(db):
    with pytest.raises(ValidationError, match="after"):
        preview(db.conn, SEPT[1], SEPT[0], 50)
    with pytest.raises(ValidationError, match="after"):
        build(db.conn, SEPT[1], SEPT[0], 50)


def test_cap_is_reported_by_preview_and_refused_by_build_never_truncated(db):
    for day in range(1, 5):
        meeting_at(db, f"2026-09-0{day}T10:00:00.000Z", title=str(day))
    assert preview(db.conn, *SEPT, 3)["over_cap"] is True and preview(db.conn, *SEPT, 4)["over_cap"] is False
    with pytest.raises(ValidationError, match="narrow"):
        build(db.conn, *SEPT, 3)
    assert len(build(db.conn, *SEPT, 4)["meetings"]) == 4


def test_empty_range(db):
    assert preview(db.conn, *SEPT, 50)["meeting_count"] == 0
    with pytest.raises(ReportEmptyError):
        build(db.conn, *SEPT, 50)


def test_summary_states(db):
    kept = meeting_at(db, "2026-09-02T10:00:00.000Z", title="ready then failed")
    with db.transaction() as tx:
        fail_summary(tx, kept.meeting_id)  # a later failure never hides the earlier ready summary
    meeting_at(db, "2026-09-03T10:00:00.000Z", title="failed only", summary="failed")
    meeting_at(db, "2026-09-04T10:00:00.000Z", title="none", summary="none")
    sections = {m["title"]: m for m in build(db.conn, *SEPT, 50)["meetings"]}
    assert sections["ready then failed"]["summary_state"] == "ready"
    assert sections["ready then failed"]["summary_text"] == "The team talked."
    assert (sections["failed only"]["summary_state"], sections["failed only"]["failure_code"]) == ("failed", "summary_invalid_output")
    assert sections["none"]["summary_state"] == "none" and sections["none"]["summary_text"] is None
    assert preview(db.conn, *SEPT, 50)["with_summary_count"] == 1


def test_stale_summary_is_flagged(db):
    seeded = meeting_at(db, "2026-09-02T10:00:00.000Z")
    assert build(db.conn, *SEPT, 50)["meetings"][0]["stale"] is False
    # A line created after the summary's input was read makes it stale, exactly as GET …/summary derives it.
    with db.transaction() as tx:
        tx.conn.execute("UPDATE AuditEvent SET payload = json_set(payload, '$.input_as_of_seq', 0) "
                        "WHERE event_type = 'summary_generated'")
        from server.audit import emit
        emit(tx, "utterance_created", "attribution", {"utterance_id": "c1a2b3c4-d5e6-4f70-8a9b-0c1d2e3f4a5b",
             "device_id": "c1a2b3c4-d5e6-4f70-8a9b-0c1d2e3f4a5c", "participant_id": None,
             "attribution_method": "device", "attribution_confidence": 0.95, "stt_confidence": 0.9}, meeting_id=seeded.meeting_id)
    from server.summary.views import is_stale
    assert is_stale(db.conn, seeded.meeting_id, summaries.current(db.conn, seeded.meeting_id)) is True
    assert build(db.conn, *SEPT, 50)["meetings"][0]["stale"] is True


def test_per_meeting_content(db):
    seeded = meeting_at(db, "2026-09-02T10:00:00.000Z", title="Sprint", people=("Priya", "Sam"),
                        items=[("Write notes", "Sam"), ("Book room", None)])
    edit_at(db, seeded.items[0], "2026-09-03T09:00:00.000Z", due_date="2026-09-10")
    [section] = build(db.conn, *SEPT, 50)["meetings"]
    assert section["participants"] == ["Priya", "Sam"] and section["ended_at"] is not None
    assert [(i["text"], i["owner"], i["status"], i["due_date"]) for i in section["action_items"]] == [
        ("Write notes", "Sam", "open", "2026-09-10"), ("Book room", "Unassigned", "open", None)]


def test_action_item_figures(db):
    old = meeting_at(db, "2026-08-10T10:00:00.000Z", title="August", items=[("aug done in range", None),
                     ("aug done before", None), ("aug done after", None), ("aug cancelled", None), ("aug reopened", None)])
    new = meeting_at(db, "2026-09-05T10:00:00.000Z", title="September", items=[("sep open", "Sam"), ("sep done", None)])
    a_in, a_before, a_after, a_cancel, a_reopen = old.items
    edit_at(db, a_in, "2026-09-12T10:00:00.000Z", status="done")
    edit_at(db, a_before, "2026-08-20T10:00:00.000Z", status="done")
    edit_at(db, a_after, "2026-10-02T10:00:00.000Z", status="done")
    edit_at(db, a_cancel, "2026-09-13T10:00:00.000Z", status="cancelled")
    edit_at(db, a_reopen, "2026-09-14T10:00:00.000Z", status="done")
    edit_at(db, a_reopen, "2026-09-15T10:00:00.000Z", status="open")   # the last change in range decides
    edit_at(db, new.items[1], "2026-09-20T10:00:00.000Z", status="done")
    edit_at(db, new.items[0], "2026-09-21T10:00:00.000Z", owner_participant_id=None)  # not a status change

    report = build(db.conn, *SEPT, 50)
    texts = lambda key: [item["text"] for item in report[key]]
    assert texts("opened") == ["sep open", "sep done"]                  # items of meetings in range
    assert texts("closed_done") == ["aug done in range", "sep done"]    # from any meeting, oldest meeting first
    assert texts("closed_cancelled") == ["aug cancelled"]
    assert texts("open_now") == ["sep open"]
    assert report["closed_done"][0]["meeting_title"] == "August"        # every figure names its meeting


def test_superseded_summary_items_are_not_counted(db):
    seeded = meeting_at(db, "2026-09-02T10:00:00.000Z", items=[("first summary item", None)])
    edit_at(db, seeded.items[0], "2026-09-03T10:00:00.000Z", status="done")
    with db.transaction() as tx:
        add_summary(tx, seeded.meeting_id, [("regenerated item", None)], seeded.people)
    report = build(db.conn, *SEPT, 50)
    assert [i["text"] for i in report["opened"]] == ["regenerated item"] and report["closed_done"] == []


def test_deleted_meetings_are_absent(db):
    gone = meeting_at(db, "2026-09-02T10:00:00.000Z", title="gone")
    meeting_at(db, "2026-09-03T10:00:00.000Z", title="kept")
    edit_at(db, gone.items[0], "2026-09-04T10:00:00.000Z", status="done")
    with db.transaction() as tx:
        registry.erase_meeting(tx, gone.meeting_id)
    report = build(db.conn, *SEPT, 50)
    assert titles(report) == ["kept"] and report["closed_done"] == []
    assert preview(db.conn, *SEPT, 50)["meeting_count"] == 1


def test_reading_writes_nothing(db):
    meeting_at(db, "2026-09-02T10:00:00.000Z")
    count = lambda: sum(db.conn.execute(f'SELECT COUNT(*) FROM "{t}"').fetchone()[0]
                        for t in ("AuditEvent", "Export", "Summary", "ActionItem"))
    before = count()
    preview(db.conn, *SEPT, 50)
    build(db.conn, *SEPT, 50)
    assert count() == before
