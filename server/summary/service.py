"""End-of-meeting summarization (CON-10, docs/summarization.md).

    trigger (meeting end or POST …/summarize) -> pending Summary + summary_started
      -> [meeting end only] drain queued STT and the attribution writes behind it (bounded)
      -> assemble the corrected transcript -> count tokens (over the limit: transcript_too_long, never truncated)
      -> generate -> validate -> [invalid: one retry with a stricter instruction] -> ready or failed

Runs once per explicit trigger and never per utterance. At most one attempt per meeting runs at a time; a second
trigger gets ``summary_in_progress``. The request returns as soon as the pending row exists; the outcome is the
``summary_generated`` / ``summary_failed`` audit event, which the dashboard hub pushes as ``summary_ready`` /
``summary_failed``. A failure only ever finishes the attempt's own ``Summary`` row: the transcript is read, never
written, and an earlier ``ready`` summary stays current. Malformed model output is never stored.

Summarization reads stored data only, so it does not depend on any phone being connected. It shares the reasoning
model with live Q&A (one generation at a time, in the adapter) and yields to STT through the compute-priority gate.
"""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass, field

from ..audit import emit
from ..config import SummaryConfig
from ..errors import SummaryInProgressError, TranscriptEmptyError
from ..ids import new_id
from ..repositories import meetings, model_executions, participants, summaries, utterances
from ..repositories.model_executions import ModelExecution
from ..timeutil import utc_now
from .prompt import TranscriptInput, assemble, build_messages
from .schema import InvalidOutput, SummaryOutput, parse_output

log = logging.getLogger("convene.summary")

# ``summary_failed.error_code`` values (docs/api.md, "Failure codes").
GENERATION_FAILED = "summary_generation_failed"
INVALID_OUTPUT = "summary_invalid_output"
TOO_LONG = "transcript_too_long"
EMPTY = "transcript_empty"


@dataclass
class Attempt:
    """The outcome of the model calls for one summary attempt (also what the reliability test records)."""
    output: SummaryOutput | None = None
    error_code: str | None = None
    error_message: str | None = None
    attempts: int = 0                                  # model calls made: 0, 1, or 2 (one retry)
    parse_errors: list[str] = field(default_factory=list)
    prompt_tokens: int | None = None
    executions: list[ModelExecution] = field(default_factory=list)
    model_error: str | None = None


def normalize_name(name: str) -> str:
    return " ".join(name.split()).casefold()


def map_owner(owner: str | None, people) -> str | None:
    """Case-insensitive exact match on display name. No match, or two participants with that name: null."""
    if owner is None:
        return None
    matches = [person.participant_id for person in people if normalize_name(person.display_name) == normalize_name(owner)]
    return matches[0] if len(matches) == 1 else None


class SummaryService:
    def __init__(self, db, config: SummaryConfig, *, reasoning=None, priority=None, pipeline=None, attribution=None):
        self.db, self.config = db, config
        self.reasoning, self.priority, self.pipeline, self.attribution = reasoning, priority, pipeline, attribution
        self._running: dict[str, str] = {}             # meeting_id -> summary_id of the attempt in progress
        self._tasks: set[asyncio.Task] = set()

    @property
    def model_identifier(self) -> str:
        return self.reasoning.model_identifier if self.reasoning is not None else "none"

    def running(self, meeting_id: str) -> str | None:
        return self._running.get(meeting_id)

    # -- lifecycle --------------------------------------------------------------------------

    async def reconcile(self) -> int:
        """Startup: an attempt left ``pending`` by a stopped server can never finish, so it is marked failed."""
        def fix(tx):
            stale = summaries.list_pending(tx.conn)
            for row in stale:
                summaries.finish(tx.conn, row.summary_id, status="failed", generated_at=utc_now(),
                                 error_message="the server stopped before this summary finished")
                emit(tx, "summary_failed", "summary", {
                    "summary_id": row.summary_id, "error_code": GENERATION_FAILED, "attempts": 0,
                    "duration_ms": None, "drain_timed_out": False}, meeting_id=row.meeting_id)
            return len(stale)
        return await self.db.run(fix)

    async def stop(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await asyncio.gather(*self._tasks, return_exceptions=True)

    # -- triggers ---------------------------------------------------------------------------

    async def summarize(self, meeting_id: str) -> dict:
        """``POST …/summarize``: start an attempt on a live or ended meeting. Never changes the meeting's state."""
        summary_id = await self._start(meeting_id, "manual")
        return {"summary_pending": True, "summary_id": summary_id}

    async def meeting_ended(self, meeting_id: str) -> None:
        """Meeting-ended hook: returns at once; the drain and the model call run in the background."""
        try:
            await self._start(meeting_id, "meeting_end")
        except TranscriptEmptyError:
            log.info("meeting %s ended with no transcript; no summary started", meeting_id)
        except SummaryInProgressError:
            # A manual attempt is already running. Lines it missed make it stale, and the view offers regenerate.
            log.info("meeting %s ended while a summary was running; not starting another", meeting_id)

    async def _start(self, meeting_id: str, trigger: str) -> str:
        if meeting_id in self._running:
            raise SummaryInProgressError("a summary attempt is already running for this meeting")
        queued = 0
        if trigger == "meeting_end" and self.pipeline is not None:
            self.pipeline.flush_meeting(meeting_id)    # the last words are queued now, and drained before reading
            queued = self.pipeline.scheduler.pending_for_meeting(meeting_id)
        summary_id = self._running[meeting_id] = new_id()

        def create(tx):
            meetings.require(tx.conn, meeting_id)
            if utterances.count_for_meeting(tx.conn, meeting_id) == 0 and queued == 0:
                raise TranscriptEmptyError("the meeting has no utterances to summarize")
            summaries.insert_pending(tx.conn, summary_id=summary_id, meeting_id=meeting_id,
                                     model_identifier=self.model_identifier)
            emit(tx, "summary_started", "summary", {"summary_id": summary_id, "trigger": trigger},
                 meeting_id=meeting_id)

        try:
            await self.db.run(create)
        except BaseException:
            self._running.pop(meeting_id, None)
            raise
        task = asyncio.create_task(self._run(meeting_id, summary_id, trigger), name=f"summary:{meeting_id}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return summary_id

    # -- one attempt ------------------------------------------------------------------------

    async def _run(self, meeting_id: str, summary_id: str, trigger: str) -> None:
        began = time.monotonic()
        drain_timed_out = False
        try:
            if trigger == "meeting_end":
                drain_timed_out = not await self._drain(meeting_id)
            transcript = await self.db.run(lambda tx: assemble(tx.conn, meeting_id))
            if transcript.utterance_count == 0:
                attempt = Attempt(error_code=EMPTY, error_message="the meeting has no transcript to summarize")
            else:
                attempt = await self.generate(transcript, summary_id)
            await self._finish(meeting_id, summary_id, transcript, attempt, began, drain_timed_out)
        except asyncio.CancelledError:
            raise   # server shutdown: the pending row is failed by reconcile() at the next start
        except Exception as exc:
            log.exception("summary %s for meeting %s failed", summary_id, meeting_id)
            attempt = Attempt(error_code=GENERATION_FAILED, error_message=f"{type(exc).__name__}: {exc}"[:300])
            try:
                await self.db.run(lambda tx: self._persist_failure(tx, meeting_id, summary_id, attempt, began,
                                                                   drain_timed_out))
            except Exception:
                log.exception("could not record the failure of summary %s", summary_id)
        finally:
            if self._running.get(meeting_id) == summary_id:
                del self._running[meeting_id]

    async def _drain(self, meeting_id: str) -> bool:
        """Wait for queued STT windows, then for the attribution writes that turn them into lines. True if both
        finished inside ``drain_timeout_s``; otherwise summarization proceeds with what exists and records it."""
        deadline = time.monotonic() + self.config.drain_timeout_s
        drained = True
        if self.pipeline is not None:
            result = await self.pipeline.drain(meeting_id, self.config.drain_timeout_s)
            if not result.drained:
                log.warning("meeting %s: %d STT window(s) still pending after %.0f s; summarizing without them",
                            meeting_id, result.remaining, self.config.drain_timeout_s)
                drained = False
        if self.attribution is not None:
            drained = await self.attribution.wait_idle(max(0.5, deadline - time.monotonic())) and drained
        return drained

    async def generate(self, transcript: TranscriptInput, related_id: str) -> Attempt:
        """Model calls for one attempt: at most one retry, and only for invalid output (a model error is final)."""
        attempt = Attempt()
        if self.reasoning is None:
            attempt.error_code, attempt.error_message = GENERATION_FAILED, "no reasoning model is loaded"
            return attempt
        loop = asyncio.get_running_loop()
        retry_reason = None
        for number in (1, 2):
            messages = build_messages(transcript, retry_reason=retry_reason)
            tokens = await loop.run_in_executor(None, self.reasoning.count_tokens, messages)
            attempt.prompt_tokens = tokens
            if tokens > self.config.max_input_tokens:
                attempt.error_code = TOO_LONG
                attempt.error_message = (f"the transcript is too long to summarize in one pass ({tokens} prompt tokens; "
                                         f"the limit is {self.config.max_input_tokens})")
                return attempt
            if self.priority is not None:
                await self.priority.wait_for_turn("reasoning")
            attempt.attempts = number
            began = time.monotonic()
            try:
                generation = await asyncio.wait_for(
                    loop.run_in_executor(None, self.reasoning.generate, messages, self.config.max_output_tokens,
                                         self.config.temperature, self.config.generation_timeout_s),
                    self.config.generation_timeout_s + 10)  # backstop: the adapter itself stops at the deadline
            except Exception as exc:
                log.exception("summary generation failed")
                attempt.model_error = f"{type(exc).__name__}: {exc}"[:500]
                attempt.error_code = GENERATION_FAILED
                attempt.error_message = f"the reasoning model failed ({type(exc).__name__})"
                return attempt
            finally:
                attempt.executions.append(ModelExecution(
                    new_id(), "reasoning", self.reasoning.model_identifier, self.reasoning.runtime,
                    round((time.monotonic() - began) * 1000), related_id, utc_now()))
            try:
                attempt.output = parse_output(generation.text)
                return attempt
            except InvalidOutput as exc:
                reason = str(exc)
                if generation.finish_reason == "length":
                    reason += ("; the reply was cut off at the length limit, so keep the summary under 250 words and "
                               "each task under 12 words")
                attempt.parse_errors.append(reason)
                log.warning("summary output rejected on attempt %d: %s", number, reason)
                retry_reason = reason
        attempt.error_code = INVALID_OUTPUT
        attempt.error_message = f"model output did not parse after one retry: {attempt.parse_errors[-1]}"
        return attempt

    # -- persistence ------------------------------------------------------------------------

    async def _finish(self, meeting_id, summary_id, transcript, attempt: Attempt, began, drain_timed_out) -> None:
        if attempt.output is None:
            await self.db.run(lambda tx: self._persist_failure(tx, meeting_id, summary_id, attempt, began,
                                                               drain_timed_out))
            return
        try:
            await self.db.run(lambda tx: self._persist_success(tx, meeting_id, summary_id, transcript, attempt, began,
                                                               drain_timed_out))
        except Exception as exc:
            # The transaction rolled back: no summary text and no action items were written.
            log.exception("could not store summary %s", summary_id)
            failed = Attempt(error_code=GENERATION_FAILED, attempts=attempt.attempts, executions=attempt.executions,
                             error_message=f"the summary could not be stored ({type(exc).__name__})")
            await self.db.run(lambda tx: self._persist_failure(tx, meeting_id, summary_id, failed, began,
                                                               drain_timed_out))

    def _persist_success(self, tx, meeting_id, summary_id, transcript, attempt: Attempt, began, drain_timed_out):
        people = participants.list_for_meeting(tx.conn, meeting_id)
        summaries.finish(tx.conn, summary_id, status="ready", summary_text=attempt.output.summary,
                         generated_at=utc_now())
        for item in attempt.output.action_items:
            summaries.insert_action_item(tx.conn, summaries.ActionItem(
                new_id(), summary_id, meeting_id, item.text, map_owner(item.owner, people), "open"))
        for execution in attempt.executions:
            model_executions.insert(tx.conn, execution)
        emit(tx, "summary_generated", "summary", {
            "summary_id": summary_id, "input_as_of_seq": transcript.input_as_of_seq,
            "model_identifier": self.model_identifier, "action_item_count": len(attempt.output.action_items),
            "attempts": attempt.attempts, "duration_ms": round((time.monotonic() - began) * 1000),
            "drain_timed_out": drain_timed_out}, meeting_id=meeting_id)

    def _persist_failure(self, tx, meeting_id, summary_id, attempt: Attempt, began, drain_timed_out):
        summaries.finish(tx.conn, summary_id, status="failed", error_message=attempt.error_message,
                         generated_at=utc_now())
        for execution in attempt.executions:
            model_executions.insert(tx.conn, execution)
        if attempt.model_error is not None:
            emit(tx, "model_error", "models", {
                "resource_type": "reasoning", "model_identifier": self.model_identifier, "device_id": None,
                "window_id": None, "related_id": summary_id, "error": attempt.model_error}, meeting_id=meeting_id)
        emit(tx, "summary_failed", "summary", {
            "summary_id": summary_id, "error_code": attempt.error_code, "attempts": attempt.attempts,
            "duration_ms": round((time.monotonic() - began) * 1000), "drain_timed_out": drain_timed_out},
            meeting_id=meeting_id)


__all__ = ["SummaryService", "Attempt", "map_owner", "GENERATION_FAILED", "INVALID_OUTPUT", "TOO_LONG", "EMPTY"]
