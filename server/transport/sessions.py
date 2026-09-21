"""Per-device transport state. One ``DeviceSession`` per device; nothing mutable is shared between them."""
import asyncio
from dataclasses import dataclass

from ..views import EMPTY_GAUGES


class TransportError(Exception):
    """A signaling-level failure with a documented code (docs/transport.md, "Errors and isolation")."""

    def __init__(self, code: str, message: str, fatal: bool = True):
        super().__init__(message)
        self.code, self.message, self.fatal = code, message, fatal


@dataclass
class DeviceStats:
    frames: int = 0
    seconds: float = 0.0  # audio received, summed frame by frame so a sample-rate change cannot skew it
    dropped_frames: int = 0  # frames dropped because the sink could not keep up
    sink_errors: int = 0
    gaps: int = 0  # audio_resumed events
    sample_rate: int | None = None
    last_audio_at: float | None = None  # time.monotonic() of the latest frame
    last_audio_wall: float | None = None


class DeviceSession:
    def __init__(self, device_id: str, meeting_id: str, queue_frames: int):
        self.device_id = device_id
        self.meeting_id = meeting_id
        self.ws = None  # the attached signaling WebSocket, if any
        self.remote_addr: str | None = None
        self.user_agent: str | None = None
        self.peer = None  # aiortc RTCPeerConnection
        self.receive_task: asyncio.Task | None = None
        self.pump_task: asyncio.Task | None = None
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=queue_frames)
        self.stats = DeviceStats()
        self.lock = asyncio.Lock()  # serializes offer handling and teardown for this device only
        self.closing = False
        self.previous_frame_at: float | None = None  # gap baseline, reset when a new peer's track starts

    @property
    def peer_state(self) -> str:
        return self.peer.connectionState if self.peer is not None else "disconnected"

    def gauges(self, now: float) -> dict:
        """Live gauges (docs/data-model.md): all null until this device has streamed audio."""
        stats = self.stats
        if stats.last_audio_at is None:
            return dict(EMPTY_GAUGES)
        return {
            "last_audio_age_ms": round((now - stats.last_audio_at) * 1000),
            "audio_duration_s": round(stats.seconds, 2),
            "stt_backlog": 0,  # no STT until CON-05
            "stt_dropped_windows": 0,
        }
