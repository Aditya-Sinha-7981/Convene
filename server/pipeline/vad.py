"""Per-device voice-activity gate: an adaptive energy detector with no dependencies.

Each device owns one ``EnergyVad``; nothing is shared between devices (docs/stt-pipeline.md). A frame is voiced
when its level is above both an absolute floor and the device's own background level plus a margin. The
background level is a low percentile of the frame levels of the last few seconds, so a phone in a noisy room
and a phone in a quiet one each adapt to their own background, *including steady noise that would otherwise
look like speech forever* (a floor that only learned from gated-out frames never adapted, which measurement
showed). It is a mitigation for silence and cross-device bleed, not a guarantee (ADR-05). All thresholds are
configuration (``[pipeline]``) and were set from measurement (logs/stt.md).
"""
from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class VadConfig:
    frame_ms: int = 20             # analysis frame length
    min_db: float = -50.0          # absolute floor: quieter than this (dBFS RMS) is never speech
    margin_db: float = 9.0         # a frame must exceed the device's background level by this much
    noise_floor_db: float = -60.0  # background estimate until enough frames have been seen
    noise_window_s: float = 5.0    # the background is estimated from this much recent audio
    noise_percentile: float = 10.0  # ...as this percentile of its frame levels (speech has pauses, noise does not)
    hangover_frames: int = 5       # keep the gate open this many frames after speech (bridges brief pauses)


class EnergyVad:
    """Frame-by-frame gate. ``process`` returns one boolean per complete frame; leftovers carry to the next call."""

    MIN_FRAMES_FOR_ESTIMATE = 10  # 0.2 s: until then the background is unknown and no frame is called speech

    def __init__(self, sample_rate: int, config: VadConfig):
        self.config = config
        self.frame_len = sample_rate * config.frame_ms // 1000
        self.noise_db = config.noise_floor_db
        self._levels: deque[float] = deque(maxlen=max(int(config.noise_window_s * 1000 / config.frame_ms), 2))
        self._leftover = np.zeros(0, dtype=np.float32)
        self._hangover = 0

    def reset_frame(self) -> None:
        """Drop the partial frame carried over from the previous chunk (used across a gap in the stream)."""
        self._leftover = np.zeros(0, dtype=np.float32)

    def process(self, samples: np.ndarray) -> list[bool]:
        """One boolean (voiced or not) per complete 20 ms frame; a partial frame carries to the next call."""
        return [voiced for voiced, _, _ in self.analyze(samples)]

    def analyze(self, samples: np.ndarray) -> list[tuple[bool, float, float]]:
        """Per complete frame: (voiced, level in dBFS, margin in dB above this device's background at that moment)."""
        data = np.concatenate([self._leftover, samples]) if len(self._leftover) else samples
        frames = len(data) // self.frame_len
        self._leftover = data[frames * self.frame_len:]
        out = []
        for i in range(frames):
            frame = data[i * self.frame_len:(i + 1) * self.frame_len]
            level = 20 * np.log10(float(np.sqrt(np.mean(frame * frame))) + 1e-10)
            self._levels.append(level)
            known = len(self._levels) >= self.MIN_FRAMES_FOR_ESTIMATE
            if known:
                self.noise_db = float(np.percentile(self._levels, self.config.noise_percentile))
            voiced = known and level > max(self.config.min_db, self.noise_db + self.config.margin_db)
            if voiced:
                self._hangover = self.config.hangover_frames
            elif self._hangover > 0:
                self._hangover -= 1
                voiced = True
            # the margin is measured against the effective floor, so a digitally silent background (noise
            # suppression can produce one) does not make every sound look enormously strong
            out.append((bool(voiced), level, level - max(self.noise_db, self.config.min_db)))
        return out
