"""Per-device windowing with a monotonic ``window_id`` and UTC timestamps.

Time base (docs/stt-pipeline.md, CON-05): each window's ``t_start`` is derived from the server wall clock at
frame receipt plus the number of samples since the anchor, and the anchor is re-taken whenever the derived
time drifts from the wall clock by more than ``resync_s``. Receipt time includes network and jitter-buffer
delay, so a timestamp is when the server *received* the audio, not when it was spoken; the accuracy is
therefore bounded by that delay and, after resync, by ``resync_s``. Windows of one device are contiguous
(``t_end`` of one is ``t_start`` of the next) except across a resync.
"""
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import numpy as np

from .vad import EnergyVad, VadConfig


def iso_utc(epoch_s: float) -> str:
    """UTC ISO 8601 with millisecond precision and a Z suffix (docs/data-model.md)."""
    return datetime.fromtimestamp(epoch_s, timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class StreamClock:
    """Maps a device's sample index to server wall-clock time (see the module docstring for its accuracy).

    The anchor is the wall time of one sample index. ``is_gap`` says whether a new chunk starts further than
    ``resync_s`` from where the sample count says it should; the caller then finishes what it holds using the old
    anchor and calls ``anchor`` to take a new one.
    """

    def __init__(self, sample_rate: int, resync_s: float):
        self.sample_rate, self.resync_s = sample_rate, resync_s
        self.anchor_wall: float | None = None
        self.anchor_index = 0

    def is_gap(self, received_before: int, chunk_len: int, t_wall_end: float) -> bool:
        if self.anchor_wall is None:
            return False
        expected = self.anchor_wall + (received_before - self.anchor_index) / self.sample_rate
        return abs(t_wall_end - chunk_len / self.sample_rate - expected) > self.resync_s

    def anchor(self, received_before: int, chunk_len: int, t_wall_end: float) -> None:
        self.anchor_wall, self.anchor_index = t_wall_end - chunk_len / self.sample_rate, received_before

    def time_at(self, sample_index: int) -> float:
        return self.anchor_wall + (sample_index - self.anchor_index) / self.sample_rate


@dataclass(frozen=True)
class Window:
    device_id: str
    meeting_id: str | None
    window_id: int
    audio: np.ndarray       # float32 mono at ``sample_rate``
    sample_rate: int
    t_start: str
    t_end: str
    speech_fraction: float  # share of the audio's 20 ms frames the VAD called speech
    created_at: float       # time.monotonic() when the window was cut, for queue-age measurement
    strength_db: float = 99.0  # mean margin of the voiced frames above the device's background (segments only)

    @property
    def n_samples(self) -> int:
        return len(self.audio)


class Windower:
    """Cuts a device's continuous 16 kHz stream into fixed windows and gates each with the device's own VAD.

    ``feed`` returns ``(passed, gated)``: windows to send to STT, and how many complete windows the VAD dropped.
    Gated windows still consume a ``window_id`` so gaps in the numbering show where the gate closed.
    """

    def __init__(self, device_id: str, meeting_id: str | None, sample_rate: int, window_ms: int, vad_config: VadConfig,
                 min_speech_fraction: float, resync_s: float = 0.25, clock=None):
        self.device_id, self.meeting_id, self.sample_rate = device_id, meeting_id, sample_rate
        self.window_samples = sample_rate * window_ms // 1000
        self.min_speech_fraction = min_speech_fraction
        self.resync_s = resync_s
        self.vad = EnergyVad(sample_rate, vad_config)
        self._clock = clock
        self._buffer: list[np.ndarray] = []
        self._buffered = 0
        self._voiced: list[bool] = []
        self._next_id = 1
        self._anchor_wall: float | None = None   # wall time of sample index 0 of the current anchor
        self._anchor_index = 0                    # samples consumed before the anchor
        self._emitted = 0                         # samples emitted in total
        self.windows_seen = self.windows_gated = self.windows_passed = 0

    def feed(self, samples: np.ndarray, t_wall_end: float) -> tuple[list[Window], int]:
        """Add samples that finished arriving at wall-clock ``t_wall_end`` (epoch seconds).

        If the chunk starts more than ``resync_s`` away from where the sample count says it should (a gap, a
        reconnect, a stall), the partial window buffered so far is flushed first, so no window straddles the gap,
        and the time anchor is re-taken from the wall clock.
        """
        if len(samples) == 0:
            return [], 0
        passed: list[Window] = []
        gated = 0
        actual_start = t_wall_end - len(samples) / self.sample_rate
        if self._anchor_wall is None:
            self._anchor_wall, self._anchor_index = actual_start, self._emitted + self._buffered
        else:
            received = self._emitted + self._buffered
            expected_start = self._anchor_wall + (received - self._anchor_index) / self.sample_rate
            if abs(actual_start - expected_start) > self.resync_s:
                passed, gated = self.flush()
                self.vad.reset_frame()
                self._anchor_wall, self._anchor_index = actual_start, self._emitted + self._buffered
        self._buffer.append(samples)
        self._buffered += len(samples)
        self._voiced.extend(self.vad.process(samples))
        cut_passed, cut_gated = self._cut()
        return passed + cut_passed, gated + cut_gated

    def flush(self) -> tuple[list[Window], int]:
        """End of stream: emit the partial final window (if at least a quarter window and voiced)."""
        if self._buffered < self.window_samples // 4:
            self._reset_buffer()
            return [], 0
        return self._emit(len(self._voiced), self._buffered)

    def _cut(self) -> tuple[list[Window], int]:
        passed, gated = [], 0
        frames_per_window = self.window_samples // self.vad.frame_len
        while self._buffered >= self.window_samples:
            window_passed, window_gated = self._emit(frames_per_window, self.window_samples)
            passed += window_passed
            gated += window_gated
        return passed, gated

    def _emit(self, frames: int, samples: int) -> tuple[list[Window], int]:
        audio = np.concatenate(self._buffer)
        window_audio, rest = audio[:samples], audio[samples:]
        flags = self._voiced[:frames]
        self._voiced = self._voiced[frames:]
        self._buffer, self._buffered = ([rest] if len(rest) else []), len(rest)
        fraction = (sum(flags) / len(flags)) if flags else 0.0
        window_id, self._next_id = self._next_id, self._next_id + 1
        start = self._anchor_wall + (self._emitted - self._anchor_index) / self.sample_rate
        self._emitted += samples
        self.windows_seen += 1
        if fraction < self.min_speech_fraction:
            self.windows_gated += 1
            return [], 1
        self.windows_passed += 1
        created = (self._clock or time.monotonic)()
        return [Window(self.device_id, self.meeting_id, window_id, window_audio, self.sample_rate, iso_utc(start),
                       iso_utc(start + samples / self.sample_rate), fraction, created)], 0

    def _reset_buffer(self) -> None:
        self._buffer, self._buffered, self._voiced = [], 0, []
