"""Per-device speech segmenting: the default input to STT (ADR-19).

Instead of cutting a device's audio every second, the segmenter follows the device's own VAD and sends one
segment per stretch of speech: it starts when speech starts (with a short pre-roll so the first word is not
clipped), ends after a pause of ``end_silence_ms`` (with a short tail), and is capped at ``max_ms``. A model call
costs about the same for 1 s of audio as for 8 s, so this sends far fewer calls than fixed windows, does not cut
words in half, and gives attribution one line per stretch of speech instead of one per second.

* A stretch that reaches the cap is cut at the quietest frame in its last ``cut_search_ms`` (usually a gap between
  words), and the rest starts the next segment, so a long monologue still produces text on time.
* A voiced burst shorter than ``min_ms`` (a click, a cough, a bump) is counted as gated, not transcribed.
* A gap in the stream (a reconnect) ends the open segment first; no segment spans it.
* Timestamps use the same time base as fixed windows (``StreamClock``): server wall clock at receipt.

Everything here is per device; nothing is shared.
"""
import time
from collections import deque
from dataclasses import dataclass

import numpy as np

from .vad import EnergyVad, VadConfig
from .windowing import StreamClock, Window, iso_utc


@dataclass(frozen=True)
class SegmentConfig:
    end_silence_ms: int = 600      # this much silence after speech ends a segment
    max_ms: int = 8000             # a segment is cut at (about) this length
    min_ms: int = 300              # less voiced audio than this is a blip, not speech
    pre_roll_ms: int = 200         # audio kept from before the first voiced frame
    tail_ms: int = 200             # silence kept after the last voiced frame
    cut_search_ms: int = 1500      # where to look for the quietest frame when the cap forces a cut
    min_speech_fraction: float = 0.2


@dataclass(slots=True)
class _Frame:
    audio: np.ndarray
    voiced: bool
    level_db: float
    margin_db: float
    index: int  # sample index of the frame's first sample


class Segmenter:
    """Same interface as ``Windower`` (``feed``, ``flush``, counters), so the pipeline can use either."""

    def __init__(self, device_id: str, meeting_id: str | None, sample_rate: int, vad_config: VadConfig,
                 config: SegmentConfig, resync_s: float = 0.25, clock=None):
        self.device_id, self.meeting_id, self.sample_rate, self.config = device_id, meeting_id, sample_rate, config
        self.vad = EnergyVad(sample_rate, vad_config)
        self.clock = StreamClock(sample_rate, resync_s)
        self._time = clock or time.monotonic
        fl = self.vad.frame_len
        ms = lambda n: max(1, round(n * sample_rate / 1000 / fl))  # noqa: E731  (milliseconds -> frames)
        self._end_silence = ms(config.end_silence_ms)
        self._max_frames = ms(config.max_ms)
        self._min_voiced = ms(config.min_ms)
        self._tail = round(config.tail_ms * sample_rate / 1000 / fl)
        self._cut_search = ms(config.cut_search_ms)
        self._pre_roll: deque[_Frame] = deque(maxlen=round(config.pre_roll_ms * sample_rate / 1000 / fl))
        self._segment: list[_Frame] = []
        self._in_speech = False
        self._trailing = 0
        self._partial = np.zeros(0, dtype=np.float32)   # samples of an incomplete frame
        self._consumed = 0                              # samples turned into frames so far
        self._next_id = 1
        self.windows_seen = self.windows_gated = self.windows_passed = 0

    # -- input ------------------------------------------------------------------------------

    def feed(self, samples: np.ndarray, t_wall_end: float) -> tuple[list[Window], int]:
        if len(samples) == 0:
            return [], 0
        out: list[Window] = []
        gated = 0
        received = self._consumed + len(self._partial)
        if self.clock.anchor_wall is None:
            self.clock.anchor(received, len(samples), t_wall_end)
        elif self.clock.is_gap(received, len(samples), t_wall_end):
            out, gated = self.flush()  # finish what the old time base covers, then re-anchor
            self.vad.reset_frame()
            self._partial = np.zeros(0, dtype=np.float32)
            self._consumed = received
            self.clock.anchor(received, len(samples), t_wall_end)
        data = np.concatenate([self._partial, samples]) if len(self._partial) else samples
        frame_len = self.vad.frame_len
        whole = len(data) // frame_len
        for i, (voiced, level, margin) in enumerate(self.vad.analyze(data[:whole * frame_len])):
            emitted, dropped = self._on_frame(_Frame(data[i * frame_len:(i + 1) * frame_len], voiced, level, margin,
                                                     self._consumed))
            self._consumed += frame_len
            out += emitted
            gated += dropped
        self._partial = data[whole * frame_len:]
        return out, gated

    def flush(self) -> tuple[list[Window], int]:
        """End of the stream, a gap, or a drain: emit the open segment (trailing silence trimmed) if it is speech."""
        out, gated = [], 0
        if self._in_speech and self._segment:
            out, gated = self._emit(self._segment[:self._end_of_speech(len(self._segment))])
        self._segment, self._in_speech, self._trailing = [], False, 0
        self._pre_roll.clear()
        return out, gated

    # -- the state machine ------------------------------------------------------------------

    def _on_frame(self, frame: _Frame) -> tuple[list[Window], int]:
        if not self._in_speech:
            if not frame.voiced:
                self._pre_roll.append(frame)
                return [], 0
            self._segment = [*self._pre_roll, frame]
            self._pre_roll.clear()
            self._in_speech, self._trailing = True, 0
            return [], 0
        self._segment.append(frame)
        self._trailing = 0 if frame.voiced else self._trailing + 1
        if self._trailing >= self._end_silence:
            frames = self._segment[:self._end_of_speech(len(self._segment))]
            self._segment, self._in_speech, self._trailing = [], False, 0
            return self._emit(frames)
        if len(self._segment) >= self._max_frames:
            return self._cut_at_the_quietest_point()
        return [], 0

    def _end_of_speech(self, length: int) -> int:
        """Index just past the last voiced frame plus the tail, within the first ``length`` frames."""
        last_voiced = max((i for i, f in enumerate(self._segment[:length]) if f.voiced), default=-1)
        return min(length, last_voiced + 1 + self._tail)

    def _cut_at_the_quietest_point(self) -> tuple[list[Window], int]:
        frames = self._segment
        lo = max(len(frames) - self._cut_search, len(frames) // 2)
        cut = min(range(lo, len(frames)), key=lambda i: frames[i].level_db)
        head, rest = frames[:cut + 1], frames[cut + 1:]
        self._segment = rest
        self._trailing = 0
        for f in reversed(rest):
            if f.voiced:
                break
            self._trailing += 1
        return self._emit(head)

    def _emit(self, frames: list[_Frame]) -> tuple[list[Window], int]:
        window_id, self._next_id = self._next_id, self._next_id + 1
        self.windows_seen += 1
        voiced = [f for f in frames if f.voiced]
        fraction = len(voiced) / len(frames) if frames else 0.0
        if len(voiced) < self._min_voiced or fraction < self.config.min_speech_fraction:
            self.windows_gated += 1
            return [], 1
        self.windows_passed += 1
        start = self.clock.time_at(frames[0].index)
        end = self.clock.time_at(frames[-1].index + self.vad.frame_len)
        strength = float(np.mean([f.margin_db for f in voiced]))
        return [Window(self.device_id, self.meeting_id, window_id, np.concatenate([f.audio for f in frames]),
                       self.sample_rate, iso_utc(start), iso_utc(end), fraction, self._time(), strength)], 0
