"""Cross-device bleed filter (ADR-32): drop a segment that is a quieter copy of another phone's speech.

When one person talks, a nearby phone can pick up the same voice. The per-device VAD (ADR-05) stops most of it, but a
loud voice can clear a neighbour's gate and appear under the wrong name. Before a segment goes to STT, this filter
compares its loudness envelope (the 20 ms frame levels the device's own VAD already measured) with every other
device of the same meeting over the same wall-clock span:

* **Same sound**: the envelopes rise and fall together. Their Pearson correlation, at the best time lag within
  ``max_lag_ms`` (devices are timestamped independently at receipt), is at least ``min_correlation``.
* **Heard louder elsewhere**: over this segment's voiced frames, the other device is at least ``min_level_gap_db``
  louder.

Both must hold to drop the segment. Two people speaking at the same time make different envelopes, so both are kept.
Equal levels (phones side by side, or automatic gain control evening them out) keep both. Every doubt keeps the
segment: missing history, too few frames, any error. A dropped segment is audited (``stt_window_dropped``, reason
``bleed``), never silent.

Each device only ever *appends* its own levels to its own history; the filter reads snapshots. A problem in one
device's history cannot stop another device's audio (it makes the filter keep the segment).
"""
from __future__ import annotations

import logging
from collections import deque
from dataclasses import dataclass
from datetime import datetime

import numpy as np

log = logging.getLogger("convene.stt")

FRAME_S = 0.02


@dataclass(frozen=True)
class BleedConfig:
    enabled: bool = True
    min_correlation: float = 0.7
    min_level_gap_db: float = 6.0
    max_lag_ms: int = 300
    history_s: float = 30.0


@dataclass(frozen=True)
class BleedVerdict:
    drop: bool
    source_device_id: str | None = None
    correlation: float | None = None
    level_gap_db: float | None = None


class LevelHistory:
    """One device's recent frame levels on the server clock: (time in epoch seconds, dBFS, voiced)."""

    def __init__(self, history_s: float = 30.0):
        self._rows: deque[tuple[float, float, bool]] = deque(maxlen=max(int(history_s / FRAME_S), 10))

    def append(self, t: float, level_db: float, voiced: bool) -> None:
        self._rows.append((t, level_db, voiced))

    def snapshot(self) -> np.ndarray:
        return np.array(self._rows, dtype=np.float64).reshape(-1, 3)


def _epoch(value: str) -> float:
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def _correlation(a: np.ndarray, b: np.ndarray) -> float:
    if len(a) < 10 or np.std(a) < 1e-6 or np.std(b) < 1e-6:
        return 0.0
    return float(np.corrcoef(a, b)[0, 1])


def judge(window, own: np.ndarray, others: dict[str, np.ndarray], config: BleedConfig) -> BleedVerdict:
    """Is ``window`` (from the device whose history is ``own``) a quieter copy of another device's audio?"""
    t0, t1 = _epoch(window.t_start), _epoch(window.t_end)
    mine = own[(own[:, 0] >= t0) & (own[:, 0] <= t1)]
    voiced = mine[:, 2] > 0.5
    if len(mine) < 10 or voiced.sum() < 5:
        return BleedVerdict(False)
    lags = np.arange(-config.max_lag_ms, config.max_lag_ms + 1, FRAME_S * 1000) / 1000.0
    best = BleedVerdict(False)
    for device_id, theirs in others.items():
        if len(theirs) < 10:
            continue
        times = theirs[:, 0]
        best_corr, best_gap = -1.0, None
        for lag in lags:
            wanted = mine[:, 0] + lag
            idx = np.clip(np.searchsorted(times, wanted), 1, len(times) - 1)
            nearest = np.where(np.abs(times[idx - 1] - wanted) <= np.abs(times[idx] - wanted), idx - 1, idx)
            present = np.abs(times[nearest] - wanted) <= FRAME_S
            if present.mean() < 0.8:
                continue  # that device has no audio for most of this span: not comparable
            corr = _correlation(mine[present, 1], theirs[nearest[present], 1])
            if corr > best_corr:
                sel = present & voiced
                if sel.sum() < 5:
                    continue
                best_corr, best_gap = corr, float(np.mean(theirs[nearest[sel], 1]) - np.mean(mine[sel, 1]))
        if best_gap is None:
            continue
        if best_corr >= config.min_correlation and best_gap >= config.min_level_gap_db:
            if not best.drop or best_gap > (best.level_gap_db or 0):
                best = BleedVerdict(True, device_id, round(best_corr, 3), round(best_gap, 1))
    return best
