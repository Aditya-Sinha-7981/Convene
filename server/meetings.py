"""Meeting and device service over the CON-03 registry: the logic behind the REST routes (docs/api.md)."""
from dataclasses import asdict
from pathlib import Path

from . import colors as palette, network, registry
from .errors import MeetingActiveError, SummaryInProgressError, ValidationError
from .repositories import audit_events, devices, exports, meetings, participants, summaries
from .summary.views import summary_view
from .views import device_view, meeting_view, participant_view


def _join_fields(runtime, meeting) -> tuple[str | None, str | None, list[str]]:
    """``join_url``, ``qr_svg`` and warnings. Null once the meeting has ended or when no LAN address is known."""
    if meeting.status == "ended":
        return None, None, []
    if runtime.host is None:
        return None, None, ["no_lan_address"]
    url = network.join_url(runtime.host, runtime.port, meeting.meeting_id)
    return url, network.qr_svg(url), []


async def create_meeting(runtime, body: dict) -> dict:
    title = body.get("title")
    if title is not None and not isinstance(title, str):
        raise ValidationError("title must be a string")
    meeting = await runtime.db.run(lambda tx: registry.create_meeting(tx, title))
    url, qr, warnings = _join_fields(runtime, meeting)
    return {"meeting": meeting_view(meeting), "join_url": url, "qr_svg": qr, "warnings": warnings}


async def get_meeting(runtime, meeting_id: str) -> dict:
    def read(tx):
        meeting = meetings.require(tx.conn, meeting_id)
        roster = devices.list_for_meeting(tx.conn, meeting_id)
        people = {d.device_id: participants.list_for_device(tx.conn, d.device_id) for d in roster}
        return (meeting, roster, people, summaries.latest_attempt(tx.conn, meeting_id),
                exports.latest_attempt(tx.conn, meeting_id), audit_events.max_seq(tx.conn))

    meeting, roster, people, latest_summary, latest_export, as_of_seq = await runtime.db.run(read)
    url, qr, _ = _join_fields(runtime, meeting)
    return {
        "meeting": meeting_view(meeting),
        "devices": [device_view(d, people[d.device_id], runtime.peers.gauges_for_device(d.device_id)) for d in roster],
        "join_url": url, "qr_svg": qr,
        "latest_summary": summary_view(latest_summary), "latest_export": asdict(latest_export) if latest_export else None,
        "summary_pending": latest_summary is not None and latest_summary.status == "pending",
        "as_of_seq": as_of_seq,
    }


async def list_history(runtime, *, status=None, q=None, from_at=None, to_at=None, limit=50, offset=0) -> dict:
    """The history list (CON-14): newest first, with participant count and summary/export flags."""
    def read(tx):
        filters = {"status": status, "q": q, "from_at": from_at, "to_at": to_at}
        result = []
        for meeting in meetings.list_meetings(tx.conn, **filters, limit=limit, offset=offset):
            count = tx.conn.execute("SELECT COUNT(*) FROM Participant WHERE meeting_id = ?",
                                    (meeting.meeting_id,)).fetchone()[0]
            result.append({**meeting_view(meeting), "participant_count": count,
                           "has_summary": summaries.current(tx.conn, meeting.meeting_id) is not None,
                           "has_export": exports.current(tx.conn, meeting.meeting_id) is not None})
        return {"meetings": result, "total": meetings.count_meetings(tx.conn, **filters)}
    return await runtime.db.run(read)


async def rename_meeting(runtime, meeting_id: str, body: dict) -> dict:
    if set(body) - {"title"} or "title" not in body:
        raise ValidationError("send exactly {\"title\": \"...\"}")
    meeting = await runtime.db.run(lambda tx: registry.rename_meeting(tx, meeting_id, body["title"]))
    return {"meeting": meeting_view(meeting)}


async def delete_meeting(runtime, meeting_id: str) -> dict:
    """Permanent deletion (explicit user action). Refused while a phone is connected or a summary is running."""
    def check(tx):
        meetings.require(tx.conn, meeting_id)
        active = [d for d in devices.list_for_meeting(tx.conn, meeting_id) if d.status in ("connected", "joining")]
        if active:
            raise MeetingActiveError(f"{len(active)} phone(s) are still connected; end the meeting first")
    await runtime.db.run(check)
    if _summary_running(runtime, meeting_id):
        raise SummaryInProgressError("a summary is being written for this meeting; delete it when that finishes")

    async def erase():
        return await runtime.db.run(lambda tx: registry.erase_meeting(tx, meeting_id))
    result = await (runtime.indexer.exclusive(meeting_id, erase) if runtime.indexer is not None else erase())
    for stored in result.export_files:  # after commit; a leftover file is only disk space, never shown again
        try:
            (runtime.settings.exports_dir / Path(stored).name).unlink(missing_ok=True)
        except OSError:
            pass
    return {"deleted": {"meeting_id": meeting_id, "utterance_count": result.utterance_count,
                        "device_count": result.device_count, "qa_query_count": result.qa_query_count}}


async def register_device(runtime, meeting_id: str, body: dict, user_agent: str | None) -> tuple[int, dict]:
    is_shared = body.get("is_shared", False)
    if not isinstance(is_shared, bool):
        raise ValidationError("is_shared must be a boolean")
    count = body.get("declared_speaker_count")
    if count is not None and (isinstance(count, bool) or not isinstance(count, int)):
        raise ValidationError("declared_speaker_count must be an integer")
    device_id = body.get("device_id")
    if not isinstance(device_id, str):
        raise ValidationError("device_id must be a UUID v4 string")
    registration = await runtime.db.run(lambda tx: registry.register_device(
        tx, meeting_id, device_id, body.get("display_name"), is_shared, count, user_agent, color=body.get("color")))
    view = device_view(registration.device, registration.participants)
    return (201 if registration.created else 200), {"device": view}


async def colors(runtime, meeting_id: str) -> dict:
    """The participant palette and which keys this meeting already uses, for the join page's picker (ADR-25)."""
    def read(tx):
        meetings.require(tx.conn, meeting_id)
        return set(participants.colors_in_meeting(tx.conn, meeting_id))
    used = await runtime.db.run(read)
    return {"colors": [{"color": key, "taken": key in used} for key in palette.PALETTE]}


async def end_meeting(runtime, meeting_id: str) -> tuple[int, dict]:
    """End a meeting: persist it, close every peer, then run the hooks. Idempotent.

    The summary hook (CON-10) starts an attempt when there is anything to summarize; ``summary_pending`` reports it.
    """
    result = await runtime.db.run(lambda tx: registry.end_meeting(tx, meeting_id))
    if result.already_ended:
        return 200, {"meeting": meeting_view(result.meeting), "summary_pending": _summary_running(runtime, meeting_id)}
    await runtime.peers.end_meeting(meeting_id)
    await runtime.run_meeting_ended_hooks(meeting_id)
    return 202, {"meeting": meeting_view(result.meeting), "summary_pending": _summary_running(runtime, meeting_id)}


def _summary_running(runtime, meeting_id: str) -> bool:
    return runtime.summary is not None and runtime.summary.running(meeting_id) is not None


__all__ = ["create_meeting", "get_meeting", "list_history", "rename_meeting", "delete_meeting", "register_device", "colors", "end_meeting", "participant_view"]
