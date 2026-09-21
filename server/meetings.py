"""Meeting and device service over the CON-03 registry: the logic behind the REST routes (docs/api.md)."""
from . import network, registry
from .errors import ValidationError
from .repositories import audit_events, devices, meetings, participants
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
        return meeting, roster, people, audit_events.max_seq(tx.conn)

    meeting, roster, people, as_of_seq = await runtime.db.run(read)
    url, qr, _ = _join_fields(runtime, meeting)
    return {
        "meeting": meeting_view(meeting),
        "devices": [device_view(d, people[d.device_id], runtime.peers.gauges_for_device(d.device_id)) for d in roster],
        "join_url": url, "qr_svg": qr,
        "latest_summary": None, "latest_export": None,  # filled by CON-10 and CON-11
        "summary_pending": False,
        "as_of_seq": as_of_seq,
    }


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
        tx, meeting_id, device_id, body.get("display_name"), is_shared, count, user_agent))
    view = device_view(registration.device, registration.participants)
    return (201 if registration.created else 200), {"device": view}


async def end_meeting(runtime, meeting_id: str) -> tuple[int, dict]:
    """End a meeting: persist it, close every peer, then run the hooks. Idempotent.

    ``summary_pending`` is false until CON-10 starts summarization from its ``on_meeting_ended`` hook.
    """
    result = await runtime.db.run(lambda tx: registry.end_meeting(tx, meeting_id))
    if result.already_ended:
        return 200, {"meeting": meeting_view(result.meeting), "summary_pending": False}
    await runtime.peers.end_meeting(meeting_id)
    await runtime.run_meeting_ended_hooks(meeting_id)
    return 202, {"meeting": meeting_view(result.meeting), "summary_pending": False}


__all__ = ["create_meeting", "get_meeting", "register_device", "end_meeting", "participant_view"]
