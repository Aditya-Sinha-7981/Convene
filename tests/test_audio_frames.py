"""Frame-format handling carried over from the DT-17 prototype: planar/packed, int/float, channels, rate."""
import numpy as np
import pytest
from av import AudioFrame

from server.transport.audio import CountingSink, frame_to_mono_int16


def frame(array, fmt, layout, rate=48000):
    f = AudioFrame.from_ndarray(np.asarray(array), format=fmt, layout=layout)
    f.sample_rate = rate
    return f


def test_mono_s16_passes_through_unchanged():
    samples = np.array([[0, 1000, -1000, 32767, -32768]], dtype=np.int16)
    pcm, rate = frame_to_mono_int16(frame(samples, "s16", "mono"))
    assert pcm.dtype == np.dtype("<i2") and pcm.ndim == 1
    assert pcm.tolist() == samples[0].tolist() and rate == 48000


def test_packed_stereo_is_averaged_per_sample_pair():
    interleaved = np.array([[1000, 3000, -2000, -4000, 500, 500]], dtype=np.int16)  # L R L R L R
    pcm, _ = frame_to_mono_int16(frame(interleaved, "s16", "stereo"))
    assert pcm.tolist() == [2000, -3000, 500]


def test_planar_stereo_is_averaged_per_sample_pair():
    planes = np.array([[1000, -2000, 500], [3000, -4000, 500]], dtype=np.int16)  # one row per channel
    pcm, _ = frame_to_mono_int16(frame(planes, "s16p", "stereo"))
    assert pcm.tolist() == [2000, -3000, 500]


def test_packed_and_planar_agree_on_the_same_audio():
    left, right = np.array([100, 200, 300, 400], dtype=np.int16), np.array([900, 800, 700, 600], dtype=np.int16)
    packed = np.stack([left, right], axis=1).reshape(1, -1)
    a, _ = frame_to_mono_int16(frame(packed, "s16", "stereo"))
    b, _ = frame_to_mono_int16(frame(np.stack([left, right]), "s16p", "stereo"))
    assert a.tolist() == b.tolist() == [500, 500, 500, 500]


def test_float_samples_are_scaled_to_int16():
    pcm, _ = frame_to_mono_int16(frame(np.array([[0.0, 0.5, -0.5, 1.0]], dtype=np.float32), "flt", "mono"))
    assert pcm.tolist() == [0, 16383, -16383, 32767]


def test_planar_float_stereo_is_averaged_then_scaled():
    planes = np.array([[0.2, -0.4], [0.6, -0.8]], dtype=np.float32)
    pcm, _ = frame_to_mono_int16(frame(planes, "fltp", "stereo"))
    assert np.allclose(pcm, [0.4 * 32767, -0.6 * 32767], atol=1)


def test_out_of_range_floats_are_clipped_not_wrapped():
    pcm, _ = frame_to_mono_int16(frame(np.array([[2.0, -2.0]], dtype=np.float32), "flt", "mono"))
    assert pcm.tolist() == [32767, -32768]


@pytest.mark.parametrize("rate", [8000, 16000, 44100, 48000])
def test_sample_rate_is_reported_as_received(rate):
    _, reported = frame_to_mono_int16(frame(np.zeros((1, 160), dtype=np.int16), "s16", "mono", rate))
    assert reported == rate


async def test_counting_sink_counts_per_device_and_drops_the_audio():
    sink = CountingSink()
    await sink.push("a", np.zeros(960, dtype="<i2"), 48000, 0.0)
    await sink.push("a", np.zeros(960, dtype="<i2"), 48000, 0.0)
    await sink.push("b", np.zeros(480, dtype="<i2"), 48000, 0.0)
    assert (sink.samples, sink.frames) == ({"a": 1920, "b": 480}, {"a": 2, "b": 1})
