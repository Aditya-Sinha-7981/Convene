"""Periodic reports across meetings (CON-18, ADR-30): read-only aggregation of stored rows. No model is called.

A range is two inclusive ``created_at`` bounds, parsed exactly like ``GET /api/meetings`` ``from``/``to``, so the
report and the history view agree on which meetings are in range. Only ``ended`` meetings are reported; the others
in range are counted as excluded. Meetings read oldest first. The figures (docs/export.md, "Periodic report"):

* **Opened in range**: the items of the current summary of every reported meeting.
* **Closed in range**: items, from any meeting, whose last status change inside the range (an
  ``action_item_updated`` event carrying ``to.status``) set them to ``done``; ``cancelled`` is reported
  separately. Only current-summary items count (ADR-28).
* **Open now**: opened items whose status is ``open`` when the report is generated, not as of the range end.
"""
import json

from ..errors import ValidationError
from ..repositories import base, meetings, participants, summaries
from ..summary.views import is_stale


class ReportEmptyError(Exception):
    """No ended meeting is in the range, so there is nothing to report."""


def _in_range(conn, from_at: str, to_at: str):
    """(ended meetings oldest first, count of created/live meetings excluded)."""
    ended = meetings.list_meetings(conn, status="ended", from_at=from_at, to_at=to_at)
    ended.reverse()
    total = meetings.count_meetings(conn, from_at=from_at, to_at=to_at)
    return ended, total - len(ended)


def _check_order(from_at: str, to_at: str) -> None:
    if from_at > to_at:
        raise ValidationError("from is after to; pick a start date on or before the end date")


def preview(conn, from_at: str, to_at: str, max_meetings: int) -> dict:
    """The counts the reports page shows before anything is rendered."""
    _check_order(from_at, to_at)
    ended, excluded = _in_range(conn, from_at, to_at)
    with_summary = sum(1 for meeting in ended if summaries.current(conn, meeting.meeting_id) is not None)
    return {"from": from_at, "to": to_at, "meeting_count": len(ended), "with_summary_count": with_summary,
            "excluded_not_ended_count": excluded, "max_meetings": max_meetings,
            "over_cap": len(ended) > max_meetings}


def _failure_code(conn, summary_id: str) -> str | None:
    row = base.query_one(conn, "SELECT json_extract(payload, '$.error_code') FROM AuditEvent "
                               "WHERE event_type = 'summary_failed' AND json_extract(payload, '$.summary_id') = ? "
                               "ORDER BY seq DESC LIMIT 1", (summary_id,))
    return row[0] if row and row[0] else None


def _item(item, names: dict, meeting) -> dict:
    return {"action_item_id": item.action_item_id, "text": item.text,
            "owner": names.get(item.owner_participant_id) or "Unassigned", "status": item.status,
            "due_date": item.due_date, "meeting_id": meeting.meeting_id, "meeting_title": meeting.title,
            "meeting_date": meeting.started_at or meeting.created_at}


def _meeting_section(conn, meeting) -> dict:
    people = participants.list_for_meeting(conn, meeting.meeting_id)
    names = {person.participant_id: person.display_name for person in people}
    current = summaries.current(conn, meeting.meeting_id)
    latest = summaries.latest_attempt(conn, meeting.meeting_id)
    if current is not None:
        state, failure = "ready", None
    elif latest is None:
        state, failure = "none", None
    elif latest.status == "failed":
        state, failure = "failed", _failure_code(conn, latest.summary_id) or "summary_failed"
    else:
        state, failure = "pending", None
    items = [_item(item, names, meeting) for item in summaries.action_items(conn, current.summary_id)] if current else []
    return {"meeting_id": meeting.meeting_id, "title": meeting.title, "created_at": meeting.created_at,
            "started_at": meeting.started_at, "ended_at": meeting.ended_at,
            "participants": [person.display_name for person in people],
            "summary_state": state, "summary_text": current.summary_text if current else None,
            "failure_code": failure, "stale": is_stale(conn, meeting.meeting_id, current),
            "action_items": items}


def _closed_in_range(conn, from_at: str, to_at: str) -> tuple[list[dict], list[dict]]:
    """(done, cancelled): the last status change of each current-summary item inside the range decides."""
    last: dict[str, str] = {}
    for row in base.query_all(conn, "SELECT payload FROM AuditEvent WHERE event_type = 'action_item_updated' "
                                    "AND timestamp >= ? AND timestamp <= ? ORDER BY seq", (from_at, to_at)):
        payload = json.loads(row[0])
        status = (payload.get("to") or {}).get("status")
        if status is not None:
            last[payload["action_item_id"]] = status
    done, cancelled = [], []
    for action_item_id, status in last.items():
        if status not in ("done", "cancelled"):
            continue
        item = summaries.get_action_item(conn, action_item_id)
        if item is None:
            continue  # its meeting was deleted (ADR-27)
        current = summaries.current(conn, item.meeting_id)
        if current is None or current.summary_id != item.summary_id:
            continue  # a superseded summary's item is never listed (ADR-28)
        meeting = meetings.require(conn, item.meeting_id)
        names = {p.participant_id: p.display_name for p in participants.list_for_meeting(conn, item.meeting_id)}
        (done if status == "done" else cancelled).append(_item(item, names, meeting))
    key = lambda entry: (entry["meeting_date"], entry["meeting_id"])
    return sorted(done, key=key), sorted(cancelled, key=key)


def build(conn, from_at: str, to_at: str, max_meetings: int) -> dict:
    """Everything the report renderer needs, as plain data. Raises before reading content when out of bounds."""
    _check_order(from_at, to_at)
    ended, excluded = _in_range(conn, from_at, to_at)
    if len(ended) > max_meetings:
        raise ValidationError(f"{len(ended)} ended meetings are in this range, more than the {max_meetings} a report "
                              "can hold; narrow the date range")
    if not ended:
        raise ReportEmptyError("no ended meetings in this date range")
    sections = [_meeting_section(conn, meeting) for meeting in ended]
    opened = [item for section in sections for item in section["action_items"]]
    done, cancelled = _closed_in_range(conn, from_at, to_at)
    return {"from": from_at, "to": to_at, "excluded_not_ended_count": excluded,
            "with_summary_count": sum(1 for s in sections if s["summary_state"] == "ready"),
            "meetings": sections, "opened": opened, "closed_done": done, "closed_cancelled": cancelled,
            "open_now": [item for item in opened if item["status"] == "open"]}
