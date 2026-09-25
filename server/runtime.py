"""Process-wide state for one running server: settings, database, transport, hub, and the meeting-ended hooks."""
import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import registry
from .audit import emit
from .attribution import AttributionService
from .config import Settings
from .dashboard_hub import DashboardHub
from .db import Database
from .errors import StorageError
from .repositories import participants
from .pipeline.pipeline import SttPipeline
from .rag.indexer import TranscriptIndexer
from .rag.qa import QAService
from .transport.audio import AudioSink, CountingSink
from .transport.peers import PeerManager

log = logging.getLogger("convene.runtime")
transcript_log = logging.getLogger("convene.transcript")

MeetingEndedHook = Callable[[str], Awaitable[None]]


@dataclass(frozen=True)
class TransportConfig:
    ws_ping_interval_s: float = 15.0   # the prototype's heartbeat
    ws_ping_timeout_s: float = 15.0
    ws_max_size: int = 1024 * 1024     # uvicorn's hard cap on one WebSocket frame
    max_message_bytes: int = 128 * 1024  # a larger text frame is answered with `invalid_message`
    max_sdp_chars: int = 100_000       # carried over from the prototype
    audio_gap_s: float = 1.5           # silence longer than this, then audio, records `audio_resumed`
    audio_queue_frames: int = 250      # about 5 s of 20 ms frames per device before the sink drops frames
    gauge_interval_s: float = 1.0
    metrics_log_interval_s: float = 5.0
    hook_timeout_s: float = 5.0


class Runtime:
    def __init__(self, settings: Settings, *, sink: AudioSink | None = None, host: str | None = None,
                 port: int = 8443, transport: TransportConfig | None = None, stt_adapter=None, stt_loaded: bool = False,
                 embedding_adapter=None, reasoning_adapter=None, reasoning_loaded: bool = False):
        if sink is not None and stt_adapter is not None:
            raise ValueError("pass either a custom audio sink or an STT adapter (the STT pipeline is the sink)")
        self.settings, self.host, self.port = settings, host, port
        self.stt_adapter, self._stt_loaded, self.embedding_adapter = stt_adapter, stt_loaded, embedding_adapter
        self.reasoning_adapter, self._reasoning_loaded = reasoning_adapter, reasoning_loaded
        self.pipeline: SttPipeline | None = None
        self._window_callbacks: list = []
        self._names: dict[str, str] = {}
        self.transport = transport or TransportConfig()
        self.sink: AudioSink = sink or CountingSink()
        self.db: Database | None = None
        self.peers: PeerManager | None = None
        self.hub: DashboardHub | None = None
        self.attribution: AttributionService | None = None
        self.indexer: TranscriptIndexer | None = None
        self.qa: QAService | None = None
        self._hooks: list[tuple[str, MeetingEndedHook]] = []
        self._log_task: asyncio.Task | None = None

    def on_transcribed_window(self, callback) -> None:
        """Register ``callback(TranscribedWindow)`` (sync or async), called for every window's outcome: ``ok``,
        ``empty``, ``failed`` or ``dropped``. Attribution (CON-06) registers here. A failing callback is logged
        and never affects the pipeline."""
        self._window_callbacks.append(callback)

    async def _dispatch_window(self, outcome) -> None:
        for callback in list(self._window_callbacks):
            try:
                value = callback(outcome)
                if asyncio.iscoroutine(value):
                    await value
            except Exception:
                log.exception("a transcribed-window callback failed")

    async def _speaker_name(self, device_id: str) -> str:
        name = self._names.get(device_id)
        if name is None:
            people = await self.db.run(lambda tx: participants.list_for_device(tx.conn, device_id))
            name = self._names[device_id] = ", ".join(p.display_name for p in people) or device_id[:8]
        return name

    async def _log_window(self, outcome) -> None:
        """One console line per transcribed segment, so a person running a meeting can see what is heard.

        Terminal only: transcripts are private and never go into a tracked file. ``log_transcripts = false`` turns it off.
        """
        try:
            who = await self._speaker_name(outcome.device_id)
        except Exception:
            who = outcome.device_id[:8]
        span = f"{outcome.t_start[11:23]}Z +{outcome.n_samples / outcome.sample_rate:.1f}s"
        if outcome.status == "ok":
            transcript_log.info("STT [%s] #%d %s conf=%.2f (queued %.0f ms, model %.0f ms): %s", who, outcome.window_id,
                                span, outcome.stt_confidence, outcome.queued_ms or 0, outcome.latency_ms or 0, outcome.text)
        elif outcome.status == "failed":
            transcript_log.warning("STT [%s] #%d %s FAILED: %s", who, outcome.window_id, span, outcome.error)
        elif outcome.status == "dropped":
            transcript_log.warning("STT [%s] #%d %s DROPPED (the STT queue was full)", who, outcome.window_id, span)

    def on_meeting_ended(self, hook: MeetingEndedHook, name: str | None = None) -> None:
        """Register ``async hook(meeting_id)``, called after a meeting is ended and its peers are closed.

        Hooks must return quickly (start long work as a task) and are cut off after ``hook_timeout_s``. A
        hook that raises or times out is audited as ``hook_failed`` and never fails the request.
        """
        self._hooks.append((name or getattr(hook, "__name__", "hook"), hook))

    async def run_meeting_ended_hooks(self, meeting_id: str) -> None:
        for name, hook in self._hooks:
            try:
                await asyncio.wait_for(hook(meeting_id), self.transport.hook_timeout_s)
            except Exception as exc:  # includes TimeoutError
                message = f"{type(exc).__name__}: {exc}"[:500]
                log.exception("meeting-ended hook %s failed", name)
                try:
                    await self.db.run(lambda tx: emit(tx, "hook_failed", "api", {"hook": name, "error": message},
                                                      meeting_id=meeting_id))
                except StorageError:
                    log.exception("could not audit the failure of hook %s", name)

    async def start(self) -> None:
        """Open the database (running migrations), reconcile after a restart, start the background tasks."""
        self.db = Database.open(self.settings.database_path)
        result = await self.db.run(registry.reconcile_after_restart)
        log.info("startup reconciliation: %d device(s) in %d meeting(s) marked disconnected",
                 result.devices, result.meetings)
        if self.stt_adapter is not None:
            await self._start_stt()
        self.peers = PeerManager(self.db, self.sink, self.transport)
        self.hub = DashboardHub(self.db, self.peers,
                                low_confidence_threshold=self.settings.attribution.low_confidence_threshold,
                                gauge_interval_s=self.transport.gauge_interval_s)
        self.db.subscribe(self.hub.on_audit)
        self.hub.start()
        self.attribution = AttributionService(self.db, self.settings.attribution)
        self.on_transcribed_window(self.attribution.accept)
        # RAG ingestion is attached only to the real transcription runtime. Test/transport-only servers retain
        # their existing no-model startup behavior; the production startup fails loudly if its local embedding
        # weights were not provisioned.
        if self.embedding_adapter is not None:
            self.indexer = TranscriptIndexer(self.db, self.embedding_adapter, self.settings.rag,
                                             priority=self.pipeline.priority if self.pipeline else None)
            await self.indexer.start()
            self.attribution.register_post_write_hook(self.indexer.enqueue)
            self.attribution.register_correction_hook(self.indexer.corrected)
            self.on_meeting_ended(self.indexer.meeting_ended, "rag-index-close")
        if self.reasoning_adapter is not None:
            await self._start_reasoning()
        # Always present so a question is always recorded; without models it answers ``failed`` with the reason.
        self.qa = QAService(self.db, self.settings.qa, embedding=self.embedding_adapter,
                            reasoning=self.reasoning_adapter, indexer=self.indexer,
                            priority=self.pipeline.priority if self.pipeline else None)
        if self.transport.metrics_log_interval_s > 0:
            self._log_task = asyncio.create_task(self._log_metrics())

    async def _start_reasoning(self) -> None:
        """Load the reasoning model now and keep it resident, so the first question has no cold start."""
        adapter = self.reasoning_adapter
        started = time.monotonic()
        if not self._reasoning_loaded:
            await asyncio.get_running_loop().run_in_executor(None, adapter.load)  # raises loudly; startup fails
        seconds = getattr(adapter, "load_seconds", None)
        duration_ms = round((seconds if seconds is not None else time.monotonic() - started) * 1000)
        await self.db.run(lambda tx: emit(tx, "model_load", "models", {
            "resource_type": "reasoning", "model_identifier": adapter.model_identifier, "runtime": adapter.runtime,
            "duration_ms": duration_ms}))

    async def _start_stt(self) -> None:
        """Load the model (unless the caller already did), record ``model_load``, and make the pipeline the sink."""
        adapter = self.stt_adapter
        started = time.monotonic()
        if not self._stt_loaded:
            await asyncio.get_running_loop().run_in_executor(None, adapter.load)  # raises loudly; startup fails
        seconds = getattr(adapter, "load_seconds", None)
        duration_ms = round((seconds if seconds is not None else time.monotonic() - started) * 1000)
        await self.db.run(lambda tx: emit(tx, "model_load", "models", {
            "resource_type": "stt", "model_identifier": adapter.model_identifier, "runtime": adapter.runtime,
            "duration_ms": duration_ms}))
        if self.settings.pipeline.log_transcripts:
            self._window_callbacks.insert(0, self._log_window)
        self.pipeline = SttPipeline(adapter, self.settings.pipeline, db=self.db, on_window=self._dispatch_window)
        self.pipeline.start()
        self.sink = self.pipeline

    async def stop(self) -> None:
        if self._log_task is not None:
            self._log_task.cancel()
            await asyncio.gather(self._log_task, return_exceptions=True)
        await self.hub.stop()
        await self.peers.shutdown()
        if self.pipeline is not None:
            await self.pipeline.stop()
            self.stt_adapter.close()
        if self.attribution is not None:
            await self.attribution.drain()
        if self.indexer is not None:
            await self.indexer.stop()
        self.db.close()

    async def _log_metrics(self) -> None:
        while True:
            await asyncio.sleep(self.transport.metrics_log_interval_s)
            for row in self.peers.diagnostics():
                age = row["last_audio_age_ms"]
                log.info("METRICS [%s] state=%s audio=%s last_audio_age_ms=%s duration_s=%s frames=%d dropped=%d gaps=%d",
                         row["device_id"][:8], row["state"],
                         "RECEIVING" if age is not None and age < 2000 else "MISSING",
                         age, row["audio_duration_s"], row["frames"], row["dropped_frames"], row["audio_gaps"])
