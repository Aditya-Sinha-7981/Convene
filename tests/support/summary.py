"""Summary test support: transcript fixtures seeded through the real registry and attribution write path."""
import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from server import registry
from server.ids import new_id
from server.pipeline.scheduler import DrainResult, TranscribedWindow
from server.repositories import meetings

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "transcripts"
BASE = datetime(2026, 9, 26, 10, 0, tzinfo=timezone.utc)
FIXTURE_NAMES = ("short_standup", "long_planning", "overlapping_speakers", "unresolved_speakers", "no_action_items",
                 "many_action_items")


def load_fixture(name: str) -> dict:
    return json.loads((FIXTURES / f"{name}.json").read_text())


def at(second: float) -> str:
    return (BASE + timedelta(seconds=second)).isoformat(timespec="milliseconds").replace("+00:00", "Z")


@dataclass
class Seeded:
    meeting_id: str
    participants: dict = field(default_factory=dict)    # display name -> participant_id
    devices: dict = field(default_factory=dict)         # speaker key -> device_id
    utterances: list = field(default_factory=list)      # Utterance rows in insertion order
    window: int = 0


async def create_meeting(db, people, *, shared_phones: int = 0) -> Seeded:
    """A meeting that started at ``BASE``, one phone per person plus ``shared_phones`` shared phones."""
    def create(tx):
        meeting = registry.create_meeting(tx, "Planning")
        meetings.update(tx.conn, meeting.meeting_id, started_at=at(0), status="live")
        seeded = Seeded(meeting.meeting_id)
        for name in people:
            registration = registry.register_device(tx, meeting.meeting_id, new_id(), name)
            seeded.devices[name] = registration.device.device_id
            seeded.participants[name] = registration.participants[0].participant_id
        for number in range(1, shared_phones + 1):
            registration = registry.register_device(tx, meeting.meeting_id, new_id(), None, True, 2)
            seeded.devices[f"shared:{number}"] = registration.device.device_id
        return seeded
    return await db.run(create)


async def say(attribution, seeded: Seeded, speaker: str, second: float, text: str):
    seeded.window += 1
    utterance = await attribution.attribute(TranscribedWindow(
        seeded.devices[speaker], seeded.meeting_id, seeded.window, "ok", text, .9, at(second), at(second + 2),
        32000, 16000))
    seeded.utterances.append(utterance)
    return utterance


async def seed_fixture(db, attribution, name: str) -> tuple[Seeded, dict]:
    fixture = load_fixture(name)
    seeded = await create_meeting(db, fixture["people"], shared_phones=fixture.get("shared_phones", 0))
    for speaker, second, text in fixture["lines"]:
        await say(attribution, seeded, speaker, second, text)
    return seeded, fixture


def summary_json(summary: str = "The team agreed to ship the beta on Friday.", items=()) -> str:
    return json.dumps({"summary": summary, "action_items": [{"text": text, "owner": owner} for text, owner in items]})


class FakePipeline:
    """Stands in for ``SttPipeline`` at meeting end: records flushes and drains; ``on_drain`` simulates late lines."""

    def __init__(self, *, pending: int = 0, drained: bool = True, on_drain=None):
        self.flushed, self.drains = [], []
        self.pending, self.drained, self.on_drain = pending, drained, on_drain
        self.scheduler = self

    def flush_meeting(self, meeting_id: str) -> None:
        self.flushed.append(meeting_id)

    def pending_for_meeting(self, meeting_id: str) -> int:
        return self.pending

    async def drain(self, meeting_id: str, timeout: float) -> DrainResult:
        self.drains.append((meeting_id, timeout))
        if self.on_drain is not None:
            await self.on_drain()
        self.pending = 0
        return DrainResult(self.drained, 0 if self.drained else 2)


class RecordingPriority:
    def __init__(self):
        self.turns = []

    async def wait_for_turn(self, kind: str = "reasoning"):
        self.turns.append(kind)
