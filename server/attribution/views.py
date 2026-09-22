from dataclasses import asdict

from ..repositories import audit_events, devices, participants
from .labels import is_low_confidence, speaker_label


def utterance_view(conn, utterance, threshold: float) -> dict:
    participant = participants.get(conn, utterance.participant_id) if utterance.participant_id else None
    ordered = devices.list_for_meeting(conn, utterance.meeting_id)
    ordinal = next(index for index, device in enumerate(ordered, 1) if device.device_id == utterance.device_id)
    view = asdict(utterance)
    view.update(seq=audit_events.utterance_created_seq(conn, utterance.utterance_id),
                speaker_label=speaker_label(utterance, participant, ordinal),
                low_confidence=is_low_confidence(utterance, threshold))
    return view
