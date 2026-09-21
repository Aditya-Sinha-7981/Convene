"""Bounded, fair scheduling of STT windows over a worker pool shared by all devices.

* One bounded FIFO queue per device. Devices with queued windows sit in a ring and workers take one window
  from the next device in the ring, so a device that floods gets one turn per rotation, exactly like a quiet
  device (round-robin, docs/stt-pipeline.md).
* Overload policy is **drop oldest**: when a device's queue is full the oldest queued window is dropped so the
  live view stays fresh. Every drop increments that device's counter, emits a ``stt_window_dropped`` audit
  event and is reported to ``on_window`` with status ``dropped``. Nothing is dropped silently.
* A window that fails (adapter error, timeout, worker fault) is reported as ``failed`` and audited as
  ``model_error``; later windows of every device continue. A worker that dies is restarted.
* Backlog (queue depth and oldest age) is published per device for the dashboard's health panel.
"""
import asyncio
import logging
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field

from ..audit import emit
from ..errors import StorageError
from ..ids import new_id
from ..repositories import model_executions
from ..timeutil import utc_now
from .adapter import SttAdapter
from .windowing import Window

log = logging.getLogger("convene.stt")


@dataclass(frozen=True)
class TranscribedWindow:
    """What the pipeline hands onward for every window that reached the scheduler."""
    device_id: str
    meeting_id: str | None
    window_id: int
    status: str            # "ok", "empty" (no speech found), "failed", or "dropped" (overload)
    text: str
    stt_confidence: float
    t_start: str
    t_end: str
    sample_rate: int
    n_samples: int
    latency_ms: float | None = None   # time spent in the model
    queued_ms: float | None = None    # time spent waiting for a worker
    error: str | None = None


@dataclass
class DeviceQueue:
    device_id: str
    meeting_id: str | None
    queue: deque = field(default_factory=deque)
    in_flight: int = 0
    in_ring: bool = False
    enqueued: int = 0
    transcribed: int = 0
    empty: int = 0
    failed: int = 0
    dropped: int = 0
    suppressed: int = 0   # hallucinated stock phrases on windows the VAD judged mostly non-speech
    last_counted: int = 0  # window_id whose outcome was last counted, so a window is never counted twice


@dataclass(frozen=True)
class DrainResult:
    drained: bool
    remaining: int


class SttScheduler:
    def __init__(self, adapter: SttAdapter, *, workers: int, queue_max: int, window_timeout_s: float, db=None,
                 on_window=None, blocklist=(), blocklist_max_speech_fraction: float = 0.5,
                 blocklist_min_strength_db: float = 10.0):
        if workers < 1 or queue_max < 1:
            raise ValueError("workers and queue_max must be at least 1")
        self.adapter, self.workers, self.queue_max = adapter, workers, queue_max
        self.window_timeout_s, self.db, self.on_window = window_timeout_s, db, on_window
        self.blocklist = frozenset(self._normalize(b) for b in blocklist)
        self.blocklist_max_speech_fraction = blocklist_max_speech_fraction
        self.blocklist_min_strength_db = blocklist_min_strength_db
        self.devices: dict[str, DeviceQueue] = {}
        self.worker_restarts = 0
        self._ring: deque[str] = deque()
        self._available = asyncio.Semaphore(0)  # one permit per queued window
        self._executor: ThreadPoolExecutor | None = None
        self._tasks: list[asyncio.Task] = []
        self._background: set[asyncio.Task] = set()

    # -- lifecycle --------------------------------------------------------------------------

    def start(self) -> None:
        self._executor = ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="convene-stt")
        self._tasks = [asyncio.create_task(self._worker(i)) for i in range(self.workers)]

    async def stop(self) -> None:
        for task in self._tasks:
            task.cancel()
        await asyncio.gather(*self._tasks, *self._background, return_exceptions=True)
        self._tasks = []
        if self._executor is not None:
            self._executor.shutdown(wait=False, cancel_futures=True)

    def _spawn(self, coro) -> None:
        task = asyncio.create_task(coro)
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    @staticmethod
    def _normalize(text: str) -> str:
        return " ".join("".join(c for c in text.lower() if c.isalnum() or c == " ").split())

    def _is_hallucination(self, text: str, window: Window) -> bool:
        """A stock phrase Whisper invents on non-speech, on audio the VAD barely accepted: a fixed window that was
        mostly non-speech, or a segment whose voiced frames were only just above the device's background."""
        weak = (window.speech_fraction < self.blocklist_max_speech_fraction
                or window.strength_db < self.blocklist_min_strength_db)
        return weak and self._normalize(text) in self.blocklist

    # -- submission -------------------------------------------------------------------------

    def _device(self, device_id: str, meeting_id: str | None) -> DeviceQueue:
        state = self.devices.get(device_id)
        if state is None:
            state = self.devices[device_id] = DeviceQueue(device_id, meeting_id)
        elif meeting_id is not None:
            state.meeting_id = meeting_id
        return state

    def submit(self, window: Window) -> None:
        """Queue a window without ever blocking. If the device's queue is full, drop its oldest window."""
        state = self._device(window.device_id, window.meeting_id)
        state.enqueued += 1
        if len(state.queue) >= self.queue_max:
            oldest = state.queue.popleft()  # the new window takes its place: the permit count is unchanged
            state.dropped += 1
            self._spawn(self._report_drop(state, oldest))
            state.queue.append(window)
            return
        state.queue.append(window)
        if not state.in_ring:
            state.in_ring = True
            self._ring.append(window.device_id)
        self._available.release()

    def _take(self) -> Window:
        """Next window in round-robin order across devices. A permit guarantees one exists."""
        device_id = self._ring.popleft()
        state = self.devices[device_id]
        window = state.queue.popleft()
        if state.queue:
            self._ring.append(device_id)
        else:
            state.in_ring = False
        state.in_flight += 1
        return window

    # -- workers ----------------------------------------------------------------------------

    async def _worker(self, index: int) -> None:
        while True:
            window = None
            try:
                await self._available.acquire()
                window = self._take()
                await self._process(window)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                # A bug in the scheduler itself, not an adapter failure. The worker carries on, and the window
                # it was holding is reported as failed rather than vanishing.
                self.worker_restarts += 1
                log.exception("STT worker %d hit an unexpected error and continues", index)
                if window is not None:
                    await self._fail_lost(window, exc)
            finally:
                if window is not None:
                    # Only after the outcome was delivered, so `drain` returning means the callbacks have run.
                    self.devices[window.device_id].in_flight -= 1

    async def _fail_lost(self, window: Window, exc: Exception) -> None:
        state = self.devices[window.device_id]
        if state.last_counted != window.window_id:
            state.failed += 1
            state.last_counted = window.window_id
        outcome = self._outcome(window, "failed", "", 0.0, 0.0, (time.monotonic() - window.created_at) * 1000,
                                f"worker error: {type(exc).__name__}: {exc}"[:500])
        try:
            await self._record(window, outcome, 0.0)
            await self._deliver(outcome)
        except Exception:
            log.exception("could not report a lost window for %s", window.device_id)

    async def _process(self, window: Window) -> None:
        state = self.devices[window.device_id]
        queued_ms = (time.monotonic() - window.created_at) * 1000
        loop = asyncio.get_running_loop()
        began = time.monotonic()
        error = None
        result = None
        try:
            result = await asyncio.wait_for(
                loop.run_in_executor(self._executor, self.adapter.transcribe_window, window.device_id,
                                     window.window_id, window.audio), self.window_timeout_s)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            error = f"timed out after {self.window_timeout_s} s"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"[:500]
        latency_ms = (time.monotonic() - began) * 1000
        state.last_counted = window.window_id

        if error is not None:
            state.failed += 1
            outcome = self._outcome(window, "failed", "", 0.0, latency_ms, queued_ms, error)
        elif not result.text.strip() or self._is_hallucination(result.text, window):
            if result.text.strip():
                state.suppressed += 1
            state.empty += 1
            outcome = self._outcome(window, "empty", "", 0.0, latency_ms, queued_ms, None)
        else:
            state.transcribed += 1
            outcome = self._outcome(window, "ok", result.text.strip(), result.stt_confidence, latency_ms, queued_ms, None)
        await self._record(window, outcome, latency_ms)
        await self._deliver(outcome)

    @staticmethod
    def _outcome(window, status, text, confidence, latency_ms, queued_ms, error) -> TranscribedWindow:
        return TranscribedWindow(window.device_id, window.meeting_id, window.window_id, status, text, confidence,
                                 window.t_start, window.t_end, window.sample_rate, window.n_samples,
                                 round(latency_ms, 1), round(queued_ms, 1), error)

    async def _record(self, window: Window, outcome: TranscribedWindow, latency_ms: float) -> None:
        """One ModelExecution row per invocation, and a model_error audit event when it failed."""
        if self.db is None:
            return
        related = f"{window.device_id}/{window.window_id}"

        def write(tx):
            model_executions.insert(tx.conn, model_executions.ModelExecution(
                new_id(), "stt", self.adapter.model_identifier, self.adapter.runtime, round(latency_ms), related, utc_now()))
            if outcome.status == "failed":
                emit(tx, "model_error", "models",
                     {"resource_type": "stt", "model_identifier": self.adapter.model_identifier,
                      "device_id": window.device_id, "window_id": window.window_id, "related_id": related,
                      "error": outcome.error}, meeting_id=window.meeting_id)
        try:
            await self.db.run(write)
        except StorageError:
            log.exception("could not record the STT invocation for %s", related)

    async def _report_drop(self, state: DeviceQueue, dropped: Window) -> None:
        if self.db is not None:
            try:
                await self.db.run(lambda tx: emit(
                    tx, "stt_window_dropped", "stt",
                    {"device_id": dropped.device_id, "window_id": dropped.window_id, "reason": "overload"},
                    meeting_id=dropped.meeting_id))
            except StorageError:
                log.exception("could not audit a dropped window for %s", dropped.device_id)
        await self._deliver(self._outcome(dropped, "dropped", "", 0.0, 0.0, (time.monotonic() - dropped.created_at) * 1000,
                                          "dropped: the queue was full"))

    async def _deliver(self, outcome: TranscribedWindow) -> None:
        if self.on_window is None:
            return
        try:
            value = self.on_window(outcome)
            if asyncio.iscoroutine(value):
                await value
        except Exception:
            log.exception("the window callback failed for %s window %d", outcome.device_id, outcome.window_id)

    # -- backlog and drain ------------------------------------------------------------------

    def backlog(self, device_id: str) -> dict:
        """Queue depth (queued plus in flight), the age of the oldest queued window, and the drop counter."""
        state = self.devices.get(device_id)
        if state is None:
            return {"depth": 0, "oldest_age_s": 0.0, "dropped": 0}
        oldest = (time.monotonic() - state.queue[0].created_at) if state.queue else 0.0
        return {"depth": len(state.queue) + state.in_flight, "oldest_age_s": round(oldest, 2), "dropped": state.dropped}

    def total_backlog(self) -> int:
        return sum(len(s.queue) + s.in_flight for s in self.devices.values())

    def pending_for_meeting(self, meeting_id: str) -> int:
        return sum(len(s.queue) + s.in_flight for s in self.devices.values() if s.meeting_id == meeting_id)

    async def drain(self, meeting_id: str, timeout: float) -> DrainResult:
        """Wait until every queued and in-flight window of the meeting has finished, or ``timeout`` seconds pass."""
        deadline = time.monotonic() + timeout
        while self.pending_for_meeting(meeting_id) > 0:
            if time.monotonic() >= deadline:
                return DrainResult(False, self.pending_for_meeting(meeting_id))
            await asyncio.sleep(0.02)
        return DrainResult(True, 0)

    def stats(self) -> dict[str, dict]:
        return {d: {"enqueued": s.enqueued, "transcribed": s.transcribed, "empty": s.empty, "failed": s.failed,
                    "dropped": s.dropped, "suppressed": s.suppressed, **self.backlog(d)} for d, s in self.devices.items()}
