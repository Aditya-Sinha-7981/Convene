"""Row dataclasses for the six entities in docs/data-model.md. Field names are the stored column names."""
import json
import sqlite3
from dataclasses import dataclass, fields


def _load(cls, row: sqlite3.Row, bools: tuple[str, ...] = ()):
    values = {f.name: row[f.name] for f in fields(cls)}
    for name in bools:
        values[name] = bool(values[name])
    return cls(**values)


@dataclass(frozen=True, slots=True)
class Meeting:
    meeting_id: str
    title: str | None
    status: str  # created | live | ended
    created_at: str
    started_at: str | None = None
    ended_at: str | None = None

    @classmethod
    def from_row(cls, row):
        return _load(cls, row)


@dataclass(frozen=True, slots=True)
class Device:
    device_id: str
    meeting_id: str
    joined_at: str
    status: str  # joining | enrolling | connected | disconnected | left
    is_shared: bool
    declared_speaker_count: int
    reconnect_count: int = 0
    user_agent: str | None = None

    @classmethod
    def from_row(cls, row):
        return _load(cls, row, bools=("is_shared",))


@dataclass(frozen=True, slots=True)
class Participant:
    participant_id: str
    meeting_id: str
    device_id: str
    display_name: str
    enrollment_status: str  # not_required | pending | enrolled | failed

    @classmethod
    def from_row(cls, row):
        return _load(cls, row)


@dataclass(frozen=True, slots=True)
class Utterance:
    utterance_id: str
    meeting_id: str
    device_id: str
    participant_id: str | None
    text: str
    t_start: str
    t_end: str
    stt_confidence: float
    attribution_method: str  # device | enrolled | generic_unresolved | manual_correction
    attribution_confidence: float
    created_at: str
    corrected: bool = False
    original_participant_id: str | None = None

    @classmethod
    def from_row(cls, row):
        return _load(cls, row, bools=("corrected",))


@dataclass(frozen=True, slots=True)
class TranscriptChunk:
    chunk_id: str
    meeting_id: str
    utterance_id_start: str
    utterance_id_end: str
    text: str
    chunk_index: int
    status: str
    created_at: str
    error_message: str | None = None
    is_closed: bool = False

    @classmethod
    def from_row(cls, row):
        return _load(cls, row, bools=("is_closed",))


@dataclass(frozen=True, slots=True)
class ConnectionEvent:
    event_id: str
    device_id: str
    meeting_id: str
    event_type: str  # connected | disconnected | reconnected | audio_resumed
    timestamp: str

    @classmethod
    def from_row(cls, row):
        return _load(cls, row)


@dataclass(frozen=True, slots=True)
class AuditEvent:
    event_id: str
    seq: int
    meeting_id: str | None
    event_type: str
    component: str
    timestamp: str
    payload: dict

    @classmethod
    def from_row(cls, row):
        values = {f.name: row[f.name] for f in fields(cls)}
        values["payload"] = json.loads(values["payload"])
        return cls(**values)
