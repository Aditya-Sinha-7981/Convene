"""Persist STT outcomes by device identity and apply atomic manual corrections."""
import asyncio
import logging
from dataclasses import dataclass
from typing import Callable

from ..audit import emit
from ..config import AttributionConfig
from ..errors import (AmbiguousDisplayNameError, ParticipantNotFoundError, UtteranceNotFoundError,
                      ValidationError)
from ..ids import is_uuid4, new_id
from ..registry import MAX_DISPLAY_NAME
from ..repositories import devices, meetings, participants, utterances
from ..repositories.models import Participant, Utterance
from ..timeutil import utc_now

log = logging.getLogger("convene.attribution")


@dataclass(frozen=True)
class CorrectionResult:
    utterance: Utterance
    participant: Participant
    created_participant: bool
    changed: bool
    wrote: bool


class AttributionService:
    def __init__(self, db, config: AttributionConfig):
        self.db, self.config = db, config
        self._post_write_hooks: list[Callable] = []
        self._correction_hooks: list[Callable] = []
        self._tasks: set[asyncio.Task] = set()

    def register_post_write_hook(self, callback: Callable) -> None:
        self._post_write_hooks.append(callback)

    def register_correction_hook(self, callback: Callable) -> None:
        self._correction_hooks.append(callback)

    def accept(self, outcome) -> None:
        """Non-blocking STT callback. Only successful, non-empty outcomes become utterances."""
        if outcome.status != "ok" or outcome.meeting_id is None or not outcome.text.strip():
            return
        self._spawn(self.attribute(outcome), "attribute")

    def _spawn(self, awaitable, name: str) -> None:
        task = asyncio.create_task(awaitable, name=f"attribution:{name}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def drain(self) -> None:
        while self._tasks:
            await asyncio.gather(*list(self._tasks), return_exceptions=True)

    async def attribute(self, outcome) -> Utterance | None:
        if outcome.status != "ok" or outcome.meeting_id is None or not outcome.text.strip():
            return None
        try:
            utterance = await self.db.run(lambda tx: self._insert(tx, outcome))
        except Exception as exc:
            log.exception("could not attribute device %s window %s", outcome.device_id, outcome.window_id)
            await self._audit_failure(outcome.meeting_id, "attribution.persist", exc)
            return None
        for hook in self._post_write_hooks:
            self._spawn(self._run_hook(hook, (utterance,), utterance.meeting_id, "attribution.post_write"),
                        "post-write-hook")
        return utterance

    def _insert(self, tx, outcome) -> Utterance:
        device = devices.require(tx.conn, outcome.device_id)
        if device.meeting_id != outcome.meeting_id:
            raise ValidationError("transcribed window meeting does not match its device")
        people = participants.list_for_device(tx.conn, device.device_id)
        if device.is_shared:
            participant_id, method, confidence = None, "generic_unresolved", self.config.unresolved_confidence
        else:
            if len(people) != 1:
                raise ValidationError("a non-shared device must have exactly one participant")
            participant_id, method, confidence = people[0].participant_id, "device", self.config.device_confidence
        utterance = utterances.insert(tx.conn, Utterance(
            utterance_id=new_id(), meeting_id=device.meeting_id, device_id=device.device_id,
            participant_id=participant_id, text=outcome.text.strip(), t_start=outcome.t_start, t_end=outcome.t_end,
            stt_confidence=outcome.stt_confidence, attribution_method=method,
            attribution_confidence=confidence, created_at=utc_now()))
        emit(tx, "utterance_created", "attribution", {
            "utterance_id": utterance.utterance_id, "device_id": utterance.device_id,
            "participant_id": utterance.participant_id, "attribution_method": method,
            "attribution_confidence": confidence, "stt_confidence": utterance.stt_confidence,
        }, meeting_id=utterance.meeting_id, timestamp=utterance.created_at)
        return utterance

    async def correct(self, meeting_id: str, utterance_id: str, target: dict) -> CorrectionResult:
        result = await self.db.run(lambda tx: self._correct(tx, meeting_id, utterance_id, target))
        if result.wrote:
            for hook in self._correction_hooks:
                self._spawn(self._run_hook(hook, (utterance_id, meeting_id), meeting_id,
                                           "attribution.correction"), "correction-hook")
        return result

    def _correct(self, tx, meeting_id: str, utterance_id: str, target: dict) -> CorrectionResult:
        meetings.require(tx.conn, meeting_id)
        current = utterances.get(tx.conn, utterance_id)
        if current is None or current.meeting_id != meeting_id:
            raise UtteranceNotFoundError(f"utterance {utterance_id} does not exist in meeting {meeting_id}")
        participant, created = self._resolve_target(tx, current, target)
        changed = current.participant_id != participant.participant_id
        if current.attribution_method == "manual_correction" and not changed:
            return CorrectionResult(current, participant, False, False, False)
        original = current.original_participant_id if current.corrected else current.participant_id
        updated = utterances.update(tx.conn, utterance_id, participant_id=participant.participant_id,
                                    attribution_method="manual_correction", attribution_confidence=1.0,
                                    corrected=True, original_participant_id=original)
        emit(tx, "utterance_corrected", "attribution", {
            "utterance_id": utterance_id,
            "from": {"participant_id": current.participant_id,
                     "attribution_method": current.attribution_method,
                     "attribution_confidence": current.attribution_confidence,
                     "corrected": current.corrected},
            "to": {"participant_id": updated.participant_id,
                   "attribution_method": updated.attribution_method,
                   "attribution_confidence": updated.attribution_confidence},
            "changed": changed,
            "created_participant_id": participant.participant_id if created else None,
        }, meeting_id=meeting_id)
        return CorrectionResult(updated, participant, created, changed, True)

    def _resolve_target(self, tx, utterance: Utterance, target: dict) -> tuple[Participant, bool]:
        if not isinstance(target, dict) or set(target) not in ({"participant_id"}, {"display_name"}):
            raise ValidationError("provide exactly one of participant_id or display_name")
        if "participant_id" in target:
            participant_id = target["participant_id"]
            if not is_uuid4(participant_id):
                raise ValidationError("participant_id must be a UUID v4")
            participant = participants.get(tx.conn, participant_id)
            if participant is None or participant.meeting_id != utterance.meeting_id:
                raise ParticipantNotFoundError(f"participant {participant_id} does not exist in this meeting")
            return participant, False
        name = target["display_name"]
        if not isinstance(name, str) or not 1 <= len(name.strip()) <= MAX_DISPLAY_NAME:
            raise ValidationError(f"display_name must be 1 to {MAX_DISPLAY_NAME} characters")
        name = name.strip()
        matches = [person for person in participants.list_for_meeting(tx.conn, utterance.meeting_id)
                   if person.display_name == name]
        if len(matches) > 1:
            raise AmbiguousDisplayNameError(f"more than one participant is named {name!r}")
        if matches:
            return matches[0], False
        participant = participants.create(tx.conn, Participant(
            participant_id=new_id(), meeting_id=utterance.meeting_id, device_id=utterance.device_id,
            display_name=name, enrollment_status="not_required"))
        return participant, True

    async def _run_hook(self, hook, args: tuple, meeting_id: str, name: str) -> None:
        try:
            value = hook(*args)
            if asyncio.iscoroutine(value):
                await value
        except Exception as exc:
            log.exception("%s hook failed", name)
            await self._audit_failure(meeting_id, name, exc)

    async def _audit_failure(self, meeting_id: str, hook: str, exc: Exception) -> None:
        try:
            message = f"{type(exc).__name__}: {exc}"[:500]
            await self.db.run(lambda tx: emit(tx, "hook_failed", "api", {"hook": hook, "error": message},
                                              meeting_id=meeting_id))
        except Exception:
            log.exception("could not audit attribution failure")
