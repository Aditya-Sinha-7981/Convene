"""Row builders for storage tests. Valid by default; override fields to break one constraint at a time."""
from dataclasses import replace

from server.ids import new_id
from server.repositories.models import Device, Meeting, Participant, Utterance

T0 = "2026-09-21T11:30:00.000Z"
T1 = "2026-09-21T11:30:01.000Z"
T2 = "2026-09-21T11:30:02.000Z"


def meeting(**over) -> Meeting:
    return replace(Meeting(meeting_id=new_id(), title="Sprint planning", status="created", created_at=T0), **over)


def device(meeting_id: str, **over) -> Device:
    return replace(Device(device_id=new_id(), meeting_id=meeting_id, joined_at=T0, status="joining",
                          is_shared=False, declared_speaker_count=1, reconnect_count=0,
                          user_agent="test-agent"), **over)


def participant(meeting_id: str, device_id: str, **over) -> Participant:
    return replace(Participant(participant_id=new_id(), meeting_id=meeting_id, device_id=device_id,
                               display_name="Priya", enrollment_status="not_required"), **over)


def utterance(meeting_id: str, device_id: str, participant_id: str | None, **over) -> Utterance:
    return replace(Utterance(utterance_id=new_id(), meeting_id=meeting_id, device_id=device_id,
                             participant_id=participant_id, text="We should ship the beta on Friday.",
                             t_start=T1, t_end=T2, stt_confidence=0.93, attribution_method="device",
                             attribution_confidence=0.95, created_at=T2), **over)
