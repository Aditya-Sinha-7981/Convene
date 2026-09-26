"""Summary API views: stored rows plus the derived fields in docs/api.md (``owner_display_name``, ``stale``)."""
from dataclasses import asdict

from ..attribution.staleness import artifact_is_stale
from ..errors import SummaryNotFoundError
from ..repositories import audit_events, meetings, participants, summaries


def summary_view(summary) -> dict | None:
    return asdict(summary) if summary is not None else None


def is_stale(conn, meeting_id: str, summary) -> bool:
    """True when a line was created or corrected after the summary's input was read (data-model.md)."""
    if summary is None:
        return False
    as_of = audit_events.summary_input_as_of_seq(conn, summary.summary_id)
    return as_of is None or artifact_is_stale(conn, meeting_id, as_of)


def summary_payload(conn, meeting_id: str) -> dict:
    """``GET /api/meetings/{id}/summary``: the current ready summary, its action items, and the latest attempt."""
    meetings.require(conn, meeting_id)
    latest = summaries.latest_attempt(conn, meeting_id)
    if latest is None:
        raise SummaryNotFoundError(f"no summary attempt exists for meeting {meeting_id}")
    current = summaries.current(conn, meeting_id)
    names = {person.participant_id: person.display_name for person in participants.list_for_meeting(conn, meeting_id)}
    items = []
    for item in summaries.action_items(conn, current.summary_id) if current is not None else []:
        view = asdict(item)
        view["owner_display_name"] = names.get(item.owner_participant_id)
        items.append(view)
    return {"summary": summary_view(current), "action_items": items, "latest_attempt": summary_view(latest),
            "stale": is_stale(conn, meeting_id, current), "summary_pending": latest.status == "pending",
            "as_of_seq": audit_events.max_seq(conn)}
