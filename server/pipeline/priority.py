"""Compute priority for the STT stage: reasoning and embedding work yields to live transcription.

MLX inference is not preemptible, so priority is cooperative and applies *before* a job starts: the reasoning
adapter (CON-09) and the embedding worker (CON-08) call ``await priority.wait_for_turn("reasoning")`` before
running. While the STT backlog (queued plus in-flight windows) is above ``high_backlog_windows`` the caller
waits, but never longer than ``max_wait_s``: a question asked during a busy moment is delayed, not starved.
A job already running is not interrupted, so this reduces interference and cannot remove it (CON-15 measures it).
"""
import asyncio
import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class Turn:
    waited_s: float
    timed_out: bool      # True: the maximum wait elapsed while the backlog was still high, so the caller proceeded anyway
    backlog: int         # the STT backlog when the caller was released


class ComputePriority:
    def __init__(self, backlog: Callable[[], int], *, high_backlog_windows: int, max_wait_s: float,
                 poll_s: float = 0.05):
        self._backlog, self.high_backlog_windows = backlog, high_backlog_windows
        self.max_wait_s, self.poll_s = max_wait_s, poll_s
        self.waits = self.timeouts = 0

    async def wait_for_turn(self, kind: str = "reasoning") -> Turn:
        """Return when STT is not backed up, or after ``max_wait_s``, whichever comes first."""
        started = time.monotonic()
        waited = False
        while self._backlog() > self.high_backlog_windows:
            waited = True
            elapsed = time.monotonic() - started
            if elapsed >= self.max_wait_s:
                self.waits, self.timeouts = self.waits + 1, self.timeouts + 1
                return Turn(elapsed, True, self._backlog())
            await asyncio.sleep(min(self.poll_s, self.max_wait_s - elapsed))
        if waited:
            self.waits += 1
        return Turn(time.monotonic() - started, False, self._backlog())
