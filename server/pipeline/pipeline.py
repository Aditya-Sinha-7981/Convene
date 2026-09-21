"""The STT stage as an ``AudioSink``: resample, gate, window, schedule, transcribe.

    transport sink.push(device_id, pcm, rate, t_wall)
        -> per-device Resampler -> Windower (per-device EnergyVad) -> SttScheduler (shared workers)
        -> on_window(TranscribedWindow)

Everything up to the scheduler is per-device state with nothing shared. The output is a callback, not a
database row: creating an ``Utterance`` is attribution's job (CON-06). No audio is persisted: windows live in
memory until transcribed.
"""
import logging

import numpy as np

from ..config import PipelineConfig
from .adapter import SttAdapter
from .audio import Resampler
from .priority import ComputePriority
from .scheduler import DrainResult, SttScheduler, TranscribedWindow
from .segmenting import SegmentConfig, Segmenter
from .vad import VadConfig
from .windowing import Windower

log = logging.getLogger("convene.stt")


def vad_config(config: PipelineConfig) -> VadConfig:
    return VadConfig(frame_ms=config.vad_frame_ms, min_db=config.vad_min_db, margin_db=config.vad_margin_db,
                     noise_floor_db=config.vad_noise_floor_db, noise_window_s=config.vad_noise_window_s,
                     noise_percentile=config.vad_noise_percentile,
                     hangover_frames=config.vad_hangover_frames)


def segment_config(config: PipelineConfig) -> SegmentConfig:
    return SegmentConfig(end_silence_ms=config.segment_end_silence_ms, max_ms=config.segment_max_ms,
                         min_ms=config.segment_min_ms, pre_roll_ms=config.segment_pre_roll_ms,
                         tail_ms=config.segment_tail_ms, cut_search_ms=config.segment_cut_search_ms,
                         min_speech_fraction=config.min_speech_fraction)


class _DeviceStage:
    """Everything that belongs to one device's audio, and nothing that belongs to another's."""

    def __init__(self, device_id: str, meeting_id: str | None, config: PipelineConfig):
        self.resampler = Resampler(config.target_rate)
        if config.segmentation == "segments":
            self.windower = Segmenter(device_id, meeting_id, config.target_rate, vad_config(config),
                                      segment_config(config), config.resync_s)
        else:
            self.windower = Windower(device_id, meeting_id, config.target_rate, config.window_ms, vad_config(config),
                                     config.min_speech_fraction, config.resync_s)
        self.frames = 0


class SttPipeline:
    def __init__(self, adapter: SttAdapter, config: PipelineConfig, *, db=None, on_window=None):
        self.adapter, self.config = adapter, config
        self._stages: dict[str, _DeviceStage] = {}
        self._meetings: dict[str, str] = {}
        self.scheduler = SttScheduler(adapter, workers=config.workers, queue_max=config.queue_max,
                                      window_timeout_s=config.window_timeout_s, db=db, on_window=on_window,
                                      blocklist=config.hallucination_blocklist,
                                      blocklist_max_speech_fraction=config.hallucination_max_speech_fraction,
                                      blocklist_min_strength_db=config.hallucination_min_strength_db)
        self.priority = ComputePriority(self.scheduler.total_backlog, high_backlog_windows=config.priority_high_backlog_windows,
                                        max_wait_s=config.priority_max_wait_s)

    def start(self) -> None:
        self.scheduler.start()

    async def stop(self) -> None:
        await self.scheduler.stop()

    def set_on_window(self, callback) -> None:
        self.scheduler.on_window = callback

    # -- AudioSink (docs/transport.md) ------------------------------------------------------

    def bind_device(self, device_id: str, meeting_id: str) -> None:
        """Tell the pipeline which meeting a device belongs to (called when the transport creates its session)."""
        self._meetings[device_id] = meeting_id
        stage = self._stages.get(device_id)
        if stage is not None:
            stage.windower.meeting_id = meeting_id

    def _stage(self, device_id: str) -> _DeviceStage:
        stage = self._stages.get(device_id)
        if stage is None:
            stage = self._stages[device_id] = _DeviceStage(device_id, self._meetings.get(device_id), self.config)
        return stage

    async def push(self, device_id: str, pcm: np.ndarray, sample_rate: int, t_wall: float) -> None:
        """Consume one decoded frame. Cheap numpy work only: nothing here waits on a model."""
        stage = self._stage(device_id)
        stage.frames += 1
        samples = stage.resampler.process(pcm, sample_rate)
        windows, _ = stage.windower.feed(samples, t_wall)
        for window in windows:
            self.scheduler.submit(window)

    def stream_ended(self, device_id: str) -> None:
        """The device's audio stopped (its peer closed): send whatever speech is still buffered."""
        stage = self._stages.get(device_id)
        if stage is not None:
            windows, _ = stage.windower.flush()
            for window in windows:
                self.scheduler.submit(window)

    def flush_meeting(self, meeting_id: str) -> None:
        """Cut and queue the partial final window of every device in the meeting."""
        for device_id, stage in self._stages.items():
            if self._meetings.get(device_id) == meeting_id:
                windows, _ = stage.windower.flush()
                for window in windows:
                    self.scheduler.submit(window)

    async def drain(self, meeting_id: str, timeout: float) -> DrainResult:
        """Flush partial windows, then wait for every queued and in-flight window of the meeting (used by CON-10)."""
        self.flush_meeting(meeting_id)
        return await self.scheduler.drain(meeting_id, timeout)

    # -- observability ----------------------------------------------------------------------

    def gauges(self, device_id: str) -> dict:
        """The STT part of a device's live gauges (docs/data-model.md)."""
        backlog = self.scheduler.backlog(device_id)
        return {"stt_backlog": backlog["depth"], "stt_dropped_windows": backlog["dropped"]}

    def device_stats(self) -> dict[str, dict]:
        out = {}
        for device_id, stage in self._stages.items():
            w = stage.windower
            out[device_id] = {"frames": stage.frames, "windows_seen": w.windows_seen, "windows_gated": w.windows_gated,
                              "windows_passed": w.windows_passed, "vad_noise_floor_db": round(w.vad.noise_db, 1),
                              **self.scheduler.stats().get(device_id, {})}
        return out


__all__ = ["SttPipeline", "TranscribedWindow", "vad_config"]
