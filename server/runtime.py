"""Process-wide state for one running server: settings, database, transport, hub, and the meeting-ended hooks."""
import asyncio
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from . import registry
from .audit import emit
from .config import Settings
from .dashboard_hub import DashboardHub
from .db import Database
from .errors import StorageError
from .transport.audio import AudioSink, CountingSink
from .transport.peers import PeerManager

log = logging.getLogger("convene.runtime")

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
                 port: int = 8443, transport: TransportConfig | None = None):
        self.settings, self.host, self.port = settings, host, port
        self.transport = transport or TransportConfig()
        self.sink: AudioSink = sink or CountingSink()
        self.db: Database | None = None
        self.peers: PeerManager | None = None
        self.hub: DashboardHub | None = None
        self._hooks: list[tuple[str, MeetingEndedHook]] = []
        self._log_task: asyncio.Task | None = None

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
        self.peers = PeerManager(self.db, self.sink, self.transport)
        self.hub = DashboardHub(self.db, self.peers, gauge_interval_s=self.transport.gauge_interval_s)
        self.db.subscribe(self.hub.on_audit)
        self.hub.start()
        if self.transport.metrics_log_interval_s > 0:
            self._log_task = asyncio.create_task(self._log_metrics())

    async def stop(self) -> None:
        if self._log_task is not None:
            self._log_task.cancel()
            await asyncio.gather(self._log_task, return_exceptions=True)
        await self.hub.stop()
        await self.peers.shutdown()
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
