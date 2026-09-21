"""The STT adapter contract, and a deterministic fake for tests.

    transcribe_window(device_id, window_id, audio) -> SttResult(text, stt_confidence)

The contract is fixed (docs/stt-pipeline.md, ADR-14): swapping the model never touches the VAD, windowing or
attribution code. ``audio`` is float32 mono at ``sample_rate`` (16 kHz). The method is synchronous and runs on
a scheduler worker thread, because inference blocks. Empty speech is ``text == ""`` with ``stt_confidence == 0.0``.
"""
import threading
import time
from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class SttResult:
    text: str
    stt_confidence: float  # 0 to 1; an uncalibrated score, not a probability (docs/data-model.md)

    def __post_init__(self):
        if not 0.0 <= self.stt_confidence <= 1.0:
            raise ValueError(f"stt_confidence must be in [0, 1], got {self.stt_confidence}")


class ModelNotProvisionedError(RuntimeError):
    """The model weights are not in the local cache. Run scripts/provision_models.py while online."""


class SttAdapter(Protocol):
    resource_type: str      # always "stt"
    model_identifier: str   # the value from configuration, for ModelExecution and model_load
    runtime: str            # "mlx", "groq" or "gemini" (docs/data-model.md)

    def load(self) -> None:
        """Load weights from the local cache only. Raises (loudly) if that is impossible; never downloads."""

    def transcribe_window(self, device_id: str, window_id: int, audio: np.ndarray) -> SttResult: ...

    def close(self) -> None: ...


class FakeAdapter:
    """Deterministic stand-in: the text is derived from the window, and delay/failure can be injected.

    ``fail_when(device_id, window_id) -> bool`` makes a call raise; ``delay_s`` (a float, or a callable of
    ``(device_id, window_id)``) makes it slow. Every call is recorded in ``calls`` for assertions.
    """

    resource_type = "stt"
    runtime = "mlx"

    def __init__(self, model_identifier: str = "fake/stt", delay_s=0.0, fail_when=None, confidence: float = 0.9,
                 silent_when=None):
        self.model_identifier = model_identifier
        self.delay_s, self.fail_when, self.confidence, self.silent_when = delay_s, fail_when, confidence, silent_when
        self.calls: list[tuple[str, int, int]] = []
        self.loaded = False
        self.closed = False
        self._lock = threading.Lock()
        self.concurrent = self.max_concurrent = 0

    def load(self) -> None:
        self.loaded = True

    def close(self) -> None:
        self.closed = True

    def transcribe_window(self, device_id: str, window_id: int, audio: np.ndarray) -> SttResult:
        with self._lock:
            self.calls.append((device_id, window_id, len(audio)))
            self.concurrent += 1
            self.max_concurrent = max(self.max_concurrent, self.concurrent)
        try:
            delay = self.delay_s(device_id, window_id) if callable(self.delay_s) else self.delay_s
            if delay:
                time.sleep(delay)
            if self.fail_when is not None and self.fail_when(device_id, window_id):
                raise RuntimeError(f"injected failure for {device_id[:8]} window {window_id}")
            if self.silent_when is not None and self.silent_when(device_id, window_id):
                return SttResult("", 0.0)
            return SttResult(f"fake {device_id[:8]} {window_id}", self.confidence)
        finally:
            with self._lock:
                self.concurrent -= 1
