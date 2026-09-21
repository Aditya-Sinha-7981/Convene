"""Speech segmenting, the default STT input (ADR-19): one segment per stretch of speech, ended by a pause.

Synthetic speech (text-to-speech) and synthetic noise: these pin the segmenter's behavior; how it behaves on real
phone audio is exactly what the manual tests in docs/manual-tests.md are for."""
import asyncio
from dataclasses import replace

import numpy as np
import pytest

from server.config import PipelineConfig
from server.pipeline.adapter import FakeAdapter
from server.pipeline.pipeline import SttPipeline, segment_config, vad_config
from server.pipeline.segmenting import SegmentConfig, Segmenter
from server.pipeline.vad import VadConfig
from server.pipeline.windowing import Windower
from tests.support.speech import RATE, at_level, clip, hum, pink, speech_stream, white
from tests.test_windowing import T0, parse, to_int16

FRAME = RATE // 50  # 20 ms


def make(config: SegmentConfig = SegmentConfig(), resync_s: float = 0.25) -> Segmenter:
    return Segmenter("dev", "meeting", RATE, VadConfig(), config, resync_s)


def run(seg: Segmenter, audio: np.ndarray, start: float = T0, chunk: int = 320):
    out = []
    for i in range(0, len(audio), chunk):
        piece = audio[i:i + chunk]
        windows, _ = seg.feed(piece, start + (i + len(piece)) / RATE)
        out += windows
    windows, _ = seg.flush()
    return out + windows


def silence(seconds: float) -> np.ndarray:
    return np.zeros(int(RATE * seconds), np.float32)


def sentences(names: list[str], gap_s: float, lead_s: float = 1.0) -> tuple[np.ndarray, list[tuple[float, float]]]:
    """The named clips separated by ``gap_s`` of silence, and where each one sits (start, end) in seconds."""
    parts, spans, t = [silence(lead_s)], [], lead_s
    for n in names:
        c = clip(n)
        spans.append((t, t + len(c) / RATE))
        parts += [c, silence(gap_s)]
        t += len(c) / RATE + gap_s
    parts.append(silence(2.0))
    return np.concatenate(parts), spans


NAMES = ["ship_beta", "release_notes", "budget", "call_client", "design_review"]


# --- one segment per stretch of speech -------------------------------------------------------


def test_each_sentence_becomes_one_segment_and_the_pauses_are_not_sent():
    audio, spans = sentences(NAMES, gap_s=1.5)
    out = run(make(), audio)
    assert len(out) == len(NAMES)
    for window, (start, end) in zip(out, spans):
        got_start, got_end = parse(window.t_start) - T0, parse(window.t_end) - T0
        assert start - 0.35 <= got_start <= start + 0.05      # begins at the speech, with a short pre-roll
        assert end - 0.05 <= got_end <= end + 0.45            # ends just after it (tail plus the gate's hangover)
        assert window.n_samples == pytest.approx((got_end - got_start) * RATE, abs=FRAME)
        assert window.speech_fraction >= 0.6 and window.sample_rate == RATE


def test_the_first_word_is_not_clipped_and_a_short_tail_is_kept():
    audio, spans = sentences(["ship_beta"], gap_s=2.0, lead_s=1.5)
    (window,) = run(make(), audio)
    start, end = spans[0]
    assert parse(window.t_start) - T0 < start - 0.1     # audio before the first voiced frame is included
    assert parse(window.t_end) - T0 > end               # and audio after the last one


def test_pre_roll_and_tail_are_configuration():
    audio, spans = sentences(["ship_beta"], gap_s=2.0, lead_s=1.5)
    (plain,) = run(make(SegmentConfig(pre_roll_ms=0, tail_ms=0)), audio)
    (padded,) = run(make(SegmentConfig(pre_roll_ms=400, tail_ms=400)), audio)
    assert padded.n_samples > plain.n_samples + int(RATE * 0.5)


def test_a_pause_shorter_than_the_end_silence_does_not_split_a_sentence():
    audio, _ = sentences(["ship_beta", "release_notes"], gap_s=0.3)
    assert len(run(make(), audio)) == 1                                     # 0.3 s: the same stretch of speech
    assert len(run(make(SegmentConfig(end_silence_ms=200)), audio)) == 2    # unless the pause setting says otherwise


def test_a_longer_pause_setting_merges_what_a_shorter_one_splits():
    audio, _ = sentences(NAMES, gap_s=0.9)
    short = run(make(SegmentConfig(end_silence_ms=300)), audio)
    long = run(make(SegmentConfig(end_silence_ms=1500, max_ms=30000)), audio)
    assert len(short) == len(NAMES) and len(long) == 1


def test_silence_and_steady_noise_produce_no_segments():
    for noise in (np.zeros(RATE * 20, np.float32), white(RATE * 20, -45), hum(RATE * 20, -35)):
        seg = make()
        assert run(seg, noise) == [] and seg.windows_passed == 0


# --- the cap ----------------------------------------------------------------------------------


def test_a_long_stretch_is_cut_at_the_cap_and_the_pieces_are_contiguous_and_lose_nothing():
    audio, _ = sentences(NAMES * 2, gap_s=0.2)                         # about 30 s of near-continuous speech
    config = SegmentConfig(max_ms=6000)
    out = run(make(config), audio)
    assert len(out) >= 4
    assert all(w.n_samples <= 6000 * RATE // 1000 + FRAME for w in out)  # never longer than the cap
    for a, b in zip(out, out[1:]):
        if b.window_id == a.window_id + 1:
            assert a.t_end == b.t_start                                # a capped cut leaves no gap and repeats nothing
    first = out[0]
    start = int(round((parse(first.t_start) - T0) * RATE))
    joined = np.concatenate([w.audio for w in out[:2]])
    assert np.array_equal(joined, audio[start:start + len(joined)])    # the audio is exactly the input, in order


def test_a_capped_cut_lands_on_a_quiet_frame_not_mid_word():
    audio, _ = sentences(NAMES * 2, gap_s=0.2)
    out = run(make(SegmentConfig(max_ms=6000)), audio)
    capped = [w for w in out if w.n_samples > 5000 * RATE // 1000]
    assert capped
    for w in capped:
        frames = w.audio[:len(w.audio) // FRAME * FRAME].reshape(-1, FRAME)
        levels = 20 * np.log10(np.sqrt((frames ** 2).mean(1)) + 1e-10)
        assert levels[-1] <= np.percentile(levels, 30), "the cut should fall in one of the segment's quietest frames"


def test_the_cap_is_configuration():
    audio, _ = sentences(NAMES, gap_s=0.2)
    assert len(run(make(SegmentConfig(max_ms=30000)), audio)) == 1
    assert len(run(make(SegmentConfig(max_ms=3000)), audio)) >= 3


# --- blips, short speech, ids -----------------------------------------------------------------


def test_a_short_voiced_burst_is_a_blip_not_speech():
    click = white(int(RATE * 0.1), -20)                                  # 100 ms of noise, like a click or a bump
    audio = np.concatenate([silence(2.0), click, silence(2.0), click, silence(2.0)])
    seg = make()
    assert run(seg, audio) == []
    assert seg.windows_gated == 2 and seg.windows_seen == 2              # counted, and each consumed an id


def test_a_short_real_word_still_passes():
    word = clip("ship_beta")[: int(RATE * 0.7)]
    out = run(make(), np.concatenate([silence(2.0), word, silence(2.0)]))
    assert len(out) == 1 and out[0].n_samples >= int(RATE * 0.7)


def test_window_ids_are_monotonic_and_gated_blips_leave_holes():
    click = white(int(RATE * 0.1), -20)
    audio = np.concatenate([silence(2.0), clip("ship_beta"), silence(2.0), click, silence(2.0), clip("budget"), silence(2.0)])
    out = run(make(), audio)
    assert [w.window_id for w in out] == [1, 3]


# --- flush and gaps ---------------------------------------------------------------------------


def test_flush_sends_the_open_segment_with_trailing_silence_trimmed_and_is_idempotent():
    seg = make()
    audio = np.concatenate([silence(1.0), clip("ship_beta")])              # the speech has not ended yet
    got = []
    for i in range(0, len(audio), 320):
        got += seg.feed(audio[i:i + 320], T0 + (i + 320) / RATE)[0]
    assert got == []                                                     # nothing yet: no pause has been heard
    windows, gated = seg.flush()
    assert len(windows) == 1 and gated == 0 and windows[0].n_samples >= int(RATE * 1.5)
    assert seg.flush() == ([], 0)


def test_a_gap_in_the_stream_ends_the_open_segment_and_no_segment_spans_it():
    seg = make()
    first = np.concatenate([silence(1.0), clip("ship_beta")])              # cut off in the middle of nothing: speech ends
    out = run_no_flush(seg, first, T0)
    out += run_no_flush(seg, np.concatenate([clip("budget"), silence(2.0)]), T0 + 40.0)   # the phone was away
    windows, _ = seg.flush()
    out += windows
    assert len(out) == 2
    a, b = out
    assert parse(a.t_end) - T0 < 5 and parse(b.t_start) - T0 == pytest.approx(40.0 - 0.2, abs=0.6)
    assert parse(b.t_start) > parse(a.t_end) + 30                        # the 35 s hole is not inside any segment


def run_no_flush(seg, audio, start):
    out = []
    for i in range(0, len(audio), 320):
        piece = audio[i:i + 320]
        out += seg.feed(piece, start + (i + len(piece)) / RATE)[0]
    return out


def test_timestamps_follow_the_wall_clock_under_jitter():
    rng = np.random.default_rng(3)
    audio, spans = sentences(NAMES, gap_s=1.5)
    seg, out = make(), []
    for i in range(0, len(audio), 320):
        piece = audio[i:i + 320]
        end = T0 + (i + len(piece)) / RATE + float(rng.uniform(-0.04, 0.04))
        out += seg.feed(piece, end)[0]
    out += seg.flush()[0]
    assert len(out) == len(NAMES)
    for window, (start, _) in zip(out, spans):
        assert abs(parse(window.t_start) - T0 - start) < 0.6


def test_chunk_sizes_do_not_change_the_result():
    audio, _ = sentences(NAMES[:3], gap_s=1.5)
    reference = run(make(), audio, chunk=320)
    for chunk in (7, 161, 1000, 4097):
        other = run(make(), audio, chunk=chunk)
        assert [w.window_id for w in other] == [w.window_id for w in reference], chunk
        assert [w.t_start for w in other] == [w.t_start for w in reference], chunk


def test_devices_share_no_state():
    a, b = make(), make()
    quiet = at_level(speech_stream(NAMES[:2]), -30)
    loud = np.concatenate([quiet, white(RATE * 20, -35)])
    run(a, quiet)
    run(b, loud)
    assert a.vad.noise_db != b.vad.noise_db and a.clock is not b.clock and a._segment is not b._segment


# --- strength (the hallucination filter's input) ----------------------------------------------


def test_speech_is_stronger_over_the_background_than_the_noise_that_slips_through():
    speech = run(make(), at_level(speech_stream(), -28))
    noise = run(make(), pink(RATE * 120, -30))
    assert speech and noise, "pink noise is the case the gate lets through; it must produce something to compare"
    assert np.median([w.strength_db for w in speech]) >= 10.0
    assert max(w.strength_db for w in noise) < 10.0


def test_a_digitally_silent_background_does_not_make_everything_look_strong():
    (window,) = run(make(), np.concatenate([silence(2.0), clip("ship_beta"), silence(2.0)]))
    assert window.strength_db < 60  # measured against the absolute floor, not against -100 dBFS


# --- the pipeline chooses the strategy from configuration ------------------------------------


def test_the_shipped_default_is_segments_and_fixed_is_one_config_line_away():
    from server.config import load_settings
    assert load_settings().pipeline.segmentation == "segments"
    for mode, cls in (("segments", Segmenter), ("fixed", Windower)):
        pipeline = SttPipeline(FakeAdapter(), replace(PipelineConfig(), segmentation=mode))
        assert isinstance(pipeline._stage("d").windower, cls)


def test_pipeline_config_maps_onto_the_segmenter():
    cfg = segment_config(PipelineConfig(segment_end_silence_ms=900, segment_max_ms=5000, segment_min_ms=250,
                                        segment_pre_roll_ms=100, segment_tail_ms=50, segment_cut_search_ms=1000,
                                        min_speech_fraction=0.3))
    assert (cfg.end_silence_ms, cfg.max_ms, cfg.min_ms, cfg.pre_roll_ms, cfg.tail_ms, cfg.cut_search_ms,
            cfg.min_speech_fraction) == (900, 5000, 250, 100, 50, 1000, 0.3)
    assert vad_config(PipelineConfig()).margin_db == 9.0


async def test_the_pipeline_in_segment_mode_transcribes_one_line_per_sentence_and_flushes_at_stream_end():
    got = []
    pipeline = SttPipeline(FakeAdapter(), PipelineConfig(), on_window=got.append)
    pipeline.start()
    try:
        pipeline.bind_device("d", "m")
        audio, _ = sentences(NAMES[:3], gap_s=1.5)
        audio = np.concatenate([audio, clip("objection")])                # the last sentence is still being spoken
        pcm = to_int16(np.interp(np.linspace(0, len(audio) - 1, len(audio) * 3), np.arange(len(audio)), audio))
        t = T0
        for i in range(0, len(pcm) - 959, 960):
            t += 0.02
            await pipeline.push("d", pcm[i:i + 960], 48000, t)            # 48 kHz frames, resampled to 16 kHz inside
        await asyncio.sleep(0.3)
        assert len(got) == 3                                              # the unfinished sentence is still buffered
        pipeline.stream_ended("d")                                        # the phone's peer closed
        assert (await pipeline.drain("m", 5)).drained
    finally:
        await pipeline.stop()
    assert len(got) == 4 and [w.window_id for w in got] == [1, 2, 3, 4] and all(w.status == "ok" for w in got)


async def test_a_sample_rate_change_in_segment_mode_keeps_ids_monotonic():
    got = []
    pipeline = SttPipeline(FakeAdapter(), PipelineConfig(), on_window=got.append)
    pipeline.start()
    try:
        pipeline.bind_device("d", "m")
        audio, _ = sentences(NAMES[:4], gap_s=1.5)
        half = len(audio) // 2
        first = to_int16(np.interp(np.linspace(0, half - 1, half * 3), np.arange(half), audio[:half]))     # 48 kHz
        second = to_int16(np.interp(np.linspace(0, len(audio) - half - 1, (len(audio) - half) * 3 // 2),
                                    np.arange(len(audio) - half), audio[half:]))                            # 24 kHz
        t = T0
        for i in range(0, len(first) - 959, 960):
            t += 0.02
            await pipeline.push("d", first[i:i + 960], 48000, t)
        for i in range(0, len(second) - 479, 480):
            t += 0.02
            await pipeline.push("d", second[i:i + 480], 24000, t)
        assert (await pipeline.drain("m", 5)).drained
    finally:
        await pipeline.stop()
    ids = [w.window_id for w in got]
    assert ids == sorted(ids) and len(ids) >= 3
