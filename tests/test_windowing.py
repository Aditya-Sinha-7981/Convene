"""Windowing, the time base, and audio preparation. Timestamps are server wall-clock at receipt (docs/stt-pipeline.md):
these tests pin their arithmetic and the stated accuracy, not the network delay a real phone adds."""
import asyncio
import re
from datetime import datetime, timezone

import numpy as np
import pytest

from server.config import PipelineConfig
from server.pipeline.audio import Resampler
from server.pipeline.pipeline import SttPipeline
from server.pipeline.adapter import FakeAdapter
from server.pipeline.vad import VadConfig
from server.pipeline.windowing import Windower, iso_utc
from tests.support.speech import RATE, sine, speech_stream, white

ISO = re.compile(r"^\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z$")
T0 = 1_800_000_000.0  # an arbitrary wall-clock start (epoch seconds)


def parse(ts: str) -> float:
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()


_SPEECH = speech_stream(lead_s=0.0, pause_s=0.05)  # speech the gate treats as speech (steady noise it rightly gates)


def loud(seconds: float, seed: int = 3) -> np.ndarray:
    """``seconds`` of speech-level audio (tiled fixture speech); ``seed`` picks a different starting offset."""
    n = int(RATE * seconds)
    return np.tile(_SPEECH, n // len(_SPEECH) + 3)[seed * 997:seed * 997 + n].copy()


def stream(windower: Windower, audio: np.ndarray, start: float = T0, chunk: int = 320, jitter=None):
    """Feed ``audio`` in chunks whose end times follow the sample count (plus optional jitter); collect windows."""
    windows, gated = [], 0
    for i in range(0, len(audio), chunk):
        piece = audio[i:i + chunk]
        end = start + (i + len(piece)) / RATE + (jitter(i) if jitter else 0.0)
        p, g = windower.feed(piece, end)
        windows += p
        gated += g
    return windows, gated


def make(window_ms=1000, min_fraction=0.2, **kw) -> Windower:
    return Windower("dev", "meeting", RATE, window_ms, VadConfig(), min_fraction, **kw)


def test_iso_utc_format():
    assert iso_utc(0) == "1970-01-01T00:00:00.000Z"
    assert ISO.match(iso_utc(T0 + 0.1234)) and iso_utc(T0 + 0.1234).endswith(".123Z")


def test_window_ids_are_monotonic_and_windows_are_exactly_the_configured_length():
    w = make()
    windows, _ = stream(w, loud(5.5))
    assert [x.window_id for x in windows] == sorted(x.window_id for x in windows) == list(range(1, len(windows) + 1))[:len(windows)]
    assert all(x.n_samples == RATE and x.sample_rate == RATE for x in windows)
    assert len(windows) + w.windows_gated == w.windows_seen == 5  # the last half second is not a complete window yet


def test_gated_windows_still_consume_an_id_so_the_numbering_shows_where_the_gate_closed():
    w = make()
    silence = np.zeros(RATE * 3, np.float32)
    windows, gated = stream(w, np.concatenate([loud(2.0), silence, loud(2.0, seed=4)]))
    ids = [x.window_id for x in windows]
    assert ids == sorted(ids) and ids[0] == 1
    assert w.windows_seen == 7 and gated == w.windows_gated >= 2
    assert max(ids) > len(ids)  # a hole in the numbering: gated windows took ids


def test_t_start_and_t_end_are_contiguous_utc_and_start_at_the_wall_clock():
    w = make()
    windows, _ = stream(w, loud(4.0))
    assert all(ISO.match(x.t_start) and ISO.match(x.t_end) for x in windows)
    assert parse(windows[0].t_start) == pytest.approx(T0, abs=0.002)  # the anchor is the wall clock at first receipt
    for a, b in zip(windows, windows[1:]):
        assert a.t_end == b.t_start
    assert all(parse(x.t_end) - parse(x.t_start) == pytest.approx(1.0, abs=0.002) for x in windows)


def test_timestamps_stay_within_the_stated_accuracy_under_network_jitter():
    """Receipt jitter of +/-40 ms moves chunk arrival times but not the sample-count timeline: windows stay
    within the resync bound (0.25 s) of the wall clock, and are never re-anchored by jitter alone."""
    rng = np.random.default_rng(11)
    jitter_by_chunk = {}
    w = make()

    def jitter(i):
        return jitter_by_chunk.setdefault(i, float(rng.uniform(-0.04, 0.04)))

    windows, _ = stream(w, loud(20.0), jitter=jitter)
    assert len(windows) == 20
    for k, win in enumerate(windows):
        assert abs(parse(win.t_start) - (T0 + k)) < 0.25, f"window {k} drifted"
    assert w._anchor_index == 0  # never resynced: jitter is below the bound


def test_a_gap_flushes_the_partial_window_and_reanchors_to_the_wall_clock():
    w = make()
    first, _ = stream(w, loud(2.5))  # two whole windows and a half-second partial
    assert len(first) == 2
    after_gap, _ = stream(w, loud(2.0, seed=5), start=T0 + 2.5 + 30.0)  # the phone was away for 30 s
    partial = after_gap[0]
    assert partial.n_samples == RATE // 2 and parse(partial.t_start) == pytest.approx(T0 + 2.0, abs=0.01)
    assert parse(partial.t_end) == pytest.approx(T0 + 2.5, abs=0.01)  # it ends where the audio stopped, not 30 s later
    resumed = after_gap[1]
    assert parse(resumed.t_start) == pytest.approx(T0 + 32.5, abs=0.02)  # and the next window starts at the wall clock
    assert all(a.window_id < b.window_id for a, b in zip(after_gap, after_gap[1:]))


def test_a_tiny_fragment_before_a_gap_is_discarded_not_sent_to_the_model():
    w = make()
    stream(w, loud(1.1))  # one window and 0.1 s left over
    windows, _ = stream(w, loud(1.0, seed=6), start=T0 + 60.0)
    assert [x.n_samples for x in windows] == [RATE] and parse(windows[0].t_start) == pytest.approx(T0 + 60.0, abs=0.02)


def test_partial_final_window_on_flush():
    w = make()
    stream(w, loud(1.6))
    windows, gated = w.flush()
    assert len(windows) == 1 and windows[0].n_samples == int(RATE * 0.6) and gated == 0
    w2 = make()
    stream(w2, loud(1.1))
    assert w2.flush() == ([], 0)  # a fragment under a quarter window is dropped


def test_flushing_a_silent_tail_is_gated_like_any_other_window():
    w = make()
    stream(w, np.concatenate([loud(1.0), np.zeros(int(RATE * 0.6), np.float32)]))
    windows, gated = w.flush()
    assert windows == [] and gated == 1


def test_chunk_sizes_that_do_not_divide_a_frame_or_window_change_nothing():
    audio = loud(6.0)
    reference, _ = stream(make(), audio, chunk=320)
    for chunk in (7, 161, 1000, 4097, 16000):
        windows, _ = stream(make(), audio, chunk=chunk)
        assert [x.window_id for x in windows] == [x.window_id for x in reference], chunk
        assert np.array_equal(np.concatenate([x.audio for x in windows]), np.concatenate([x.audio for x in reference]))


def test_no_audio_is_lost_or_duplicated_between_windows():
    audio = loud(5.0)
    windows, _ = stream(make(), audio)
    assert np.array_equal(np.concatenate([x.audio for x in windows]), audio[:len(windows) * RATE])


def test_the_window_length_is_configuration():
    windows, _ = stream(make(window_ms=2000), loud(6.0))
    assert len(windows) == 3 and all(x.n_samples == 2 * RATE for x in windows)


def test_devices_have_separate_counters_ids_and_clocks():
    a, b = make(), make()
    stream(a, loud(3.0), start=T0)
    windows_b, _ = stream(b, loud(2.0, seed=9), start=T0 + 500)
    assert a.windows_seen == 3 and b.windows_seen == 2
    assert windows_b[0].window_id == 1 and parse(windows_b[0].t_start) == pytest.approx(T0 + 500, abs=0.002)


def test_two_devices_speaking_a_second_apart_order_correctly_by_t_start():
    """Cross-device ordering by t_start relies on the shared server clock (docs/api.md, ordering rule)."""
    a, b = make(), make()
    wa, _ = stream(a, np.concatenate([loud(2.0), np.zeros(RATE * 2, np.float32)]), start=T0)
    wb, _ = stream(b, np.concatenate([np.zeros(RATE, np.float32), loud(2.0, seed=8), np.zeros(RATE, np.float32)]), start=T0)
    a_first, b_first = min(wa, key=lambda x: x.t_start), min(wb, key=lambda x: x.t_start)
    assert parse(a_first.t_start) < parse(b_first.t_start) and parse(b_first.t_start) - parse(a_first.t_start) == pytest.approx(1.0, abs=0.05)


# --- resampling ------------------------------------------------------------------------------


def to_int16(x: np.ndarray) -> np.ndarray:
    return np.clip(x * 32767, -32768, 32767).astype("<i2")


def dominant_hz(x: np.ndarray, rate: int) -> float:
    spectrum = np.abs(np.fft.rfft(x * np.hanning(len(x))))
    return float(np.argmax(spectrum) * rate / len(x))


def resample_in_chunks(resampler, audio_i16, rate, chunk):
    return np.concatenate([resampler.process(audio_i16[i:i + chunk], rate) for i in range(0, len(audio_i16), chunk)])


def test_48k_to_16k_keeps_duration_pitch_and_level():
    src = to_int16(sine(1000, 2.0, -20, rate=48000))
    out = resample_in_chunks(Resampler(16000), src, 48000, 960)  # 20 ms frames, as the transport delivers
    assert abs(len(out) - 32000) < 400  # two seconds, within the converter's filter delay
    assert out.dtype == np.float32 and abs(out).max() <= 1.0
    assert dominant_hz(out, 16000) == pytest.approx(1000, abs=5)
    assert 20 * np.log10(np.sqrt(np.mean(out[2000:-2000] ** 2))) == pytest.approx(-20, abs=1.0)


@pytest.mark.parametrize("rate", [8000, 16000, 24000, 44100, 48000])
def test_any_input_rate_comes_out_at_the_model_rate(rate):
    out = resample_in_chunks(Resampler(16000), to_int16(sine(500, 1.5, -20, rate=rate)), rate, rate // 50)
    assert abs(len(out) - 24000) < 500
    assert dominant_hz(out[1000:], 16000) == pytest.approx(500, abs=8)


def test_very_short_chunks_are_buffered_not_lost():
    src = to_int16(sine(700, 1.0, -20, rate=48000))
    out = resample_in_chunks(Resampler(16000), src, 48000, 10)  # 10-sample chunks
    assert abs(len(out) - 16000) < 400


def test_empty_and_silent_input():
    r = Resampler(16000)
    assert len(r.process(np.zeros(0, dtype="<i2"), 48000)) == 0
    out = r.process(np.zeros(4800, dtype="<i2"), 48000)
    assert not np.any(out)


def test_a_rate_change_mid_stream_starts_a_new_converter_instead_of_mixing_rates():
    r = Resampler(16000)
    a = resample_in_chunks(r, to_int16(sine(1000, 1.0, -20, rate=48000)), 48000, 960)
    b = resample_in_chunks(r, to_int16(sine(1000, 1.0, -20, rate=24000)), 24000, 480)
    assert abs(len(a) - 16000) < 400 and abs(len(b) - 16000) < 400
    assert dominant_hz(a[1000:], 16000) == pytest.approx(1000, abs=8) and dominant_hz(b[1000:], 16000) == pytest.approx(1000, abs=8)


def test_audio_already_at_the_model_rate_is_only_rescaled():
    src = np.array([0, 16384, -16384, 32767, -32768], dtype="<i2")
    assert np.allclose(Resampler(16000).process(src, 16000), src / 32768.0)


# --- through the pipeline, with a sample-rate change ----------------------------------------


async def test_the_pipeline_handles_a_sample_rate_change_and_keeps_window_ids_monotonic():
    got = []
    pipeline = SttPipeline(FakeAdapter(), PipelineConfig(segmentation="fixed"), on_window=got.append)
    pipeline.start()

    def at_rate(x, rate):
        return to_int16(np.interp(np.linspace(0, len(x) - 1, int(len(x) * rate / RATE)), np.arange(len(x)), x))

    try:
        pipeline.bind_device("d", "m")
        t = T0
        first, second = at_rate(loud(3.0), 48000), at_rate(loud(3.0, seed=4), 24000)
        for i in range(0, len(first) - 959, 960):  # 3 s at 48 kHz in 20 ms frames
            t += 0.02
            await pipeline.push("d", first[i:i + 960], 48000, t)
        for i in range(0, len(second) - 479, 480):  # the stream renegotiates to 24 kHz
            t += 0.02
            await pipeline.push("d", second[i:i + 480], 24000, t)
        result = await pipeline.drain("m", 5)
    finally:
        await pipeline.stop()
    ids = [w.window_id for w in got]
    assert result.drained and ids == sorted(ids) and len(ids) >= 5
    stats = pipeline.device_stats()["d"]
    assert stats["windows_seen"] >= 5 and stats["windows_passed"] >= 5


async def test_drain_flushes_the_partial_final_window_so_the_last_words_are_not_lost():
    """Summarization drains before reading the transcript: speech that has not filled a window yet must be sent."""
    got = []
    pipeline = SttPipeline(FakeAdapter(), PipelineConfig(segmentation="fixed"), on_window=got.append)
    pipeline.start()
    try:
        pipeline.bind_device("d", "m")
        audio = to_int16(np.interp(np.linspace(0, 24000 - 1, 72000), np.arange(24000), loud(1.5)))  # 1.5 s, at 48 kHz
        t = T0
        for i in range(0, len(audio) - 959, 960):
            t += 0.02
            await pipeline.push("d", audio[i:i + 960], 48000, t)
        await asyncio.sleep(0.2)
        before = len(got)
        assert before == 1  # one whole second went out; the half second is still buffered
        result = await pipeline.drain("m", 5)
    finally:
        await pipeline.stop()
    assert result.drained and len(got) == 2
    assert got[1].n_samples < RATE and got[1].n_samples >= RATE // 4 and got[1].window_id == 2
    assert await_free(pipeline)


def await_free(pipeline) -> bool:
    return pipeline.scheduler.total_backlog() == 0
