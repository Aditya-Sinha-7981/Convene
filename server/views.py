"""API view builders: stored rows plus the derived fields documented in docs/api.md ("Derived fields")."""
from dataclasses import asdict

from .repositories.models import Device, Meeting, Participant

GAUGE_KEYS = ("last_audio_age_ms", "audio_duration_s", "stt_backlog", "stt_dropped_windows")
EMPTY_GAUGES = {key: None for key in GAUGE_KEYS}


def meeting_view(meeting: Meeting) -> dict:
    return asdict(meeting)


def participant_view(participant: Participant) -> dict:
    return asdict(participant)


def device_view(device: Device, participants: list[Participant], gauges: dict | None = None) -> dict:
    view = asdict(device)
    view["participants"] = [participant_view(p) for p in participants]
    if gauges is not None:
        view["gauges"] = gauges
    return view
