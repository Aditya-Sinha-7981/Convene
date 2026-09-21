"""Audio preparation for the STT stage: mono int16 at the receive rate -> float32 at the model rate.

One ``Resampler`` per device (its filter state is per stream, so devices never share state). PyAV, which ships
with aiortc, does the sample-rate conversion; a change of the incoming rate mid-stream starts a new
converter rather than mixing rates.
"""
import av
import numpy as np


class Resampler:
    def __init__(self, target_rate: int):
        self.target_rate = target_rate
        self._source_rate: int | None = None
        self._converter: av.AudioResampler | None = None

    def process(self, pcm: np.ndarray, sample_rate: int) -> np.ndarray:
        """Convert one chunk of mono int16 samples to float32 in [-1, 1] at ``target_rate``.

        Very short chunks are fine (the converter buffers what it cannot emit yet). Empty input returns empty
        output. A different ``sample_rate`` than the previous chunk resets the converter.
        """
        if len(pcm) == 0:
            return np.zeros(0, dtype=np.float32)
        if sample_rate == self.target_rate:
            self._converter, self._source_rate = None, sample_rate
            return pcm.astype(np.float32) / 32768.0
        if self._converter is None or sample_rate != self._source_rate:
            self._converter = av.AudioResampler(format="s16", layout="mono", rate=self.target_rate)
            self._source_rate = sample_rate
        frame = av.AudioFrame.from_ndarray(np.ascontiguousarray(pcm, dtype="<i2").reshape(1, -1),
                                           format="s16", layout="mono")
        frame.sample_rate = sample_rate
        out = [f.to_ndarray().reshape(-1) for f in self._converter.resample(frame)]
        if not out:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(out).astype(np.float32) / 32768.0
