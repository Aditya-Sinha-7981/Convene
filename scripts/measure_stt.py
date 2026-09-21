"""Measure the STT stage on this machine. Results are only valid on the reference laptop (M4 Pro, 24 GB).

    .venv/bin/python scripts/measure_stt.py models [--candidate REPO ...] [--windows 1000 2000 3000]

For each candidate model (default: the two in docs/models.md), in its own subprocess so memory is clean:
load time, peak resident memory, per-window latency, and word error rate when the fixture clips are cut into
fixed windows of each length and the window transcripts are joined. Input is the synthetic speech in
tests/fixtures/audio, so the WER shows how models and window sizes compare, NOT accuracy on real phone audio.

    .venv/bin/python scripts/measure_stt.py pipeline [--devices 1 2 5] [--seconds 40] [--window-ms 1000]

The `pipeline` command runs N synthetic phones through the real server and the real model and reports, per
scenario ("continuous": everyone talks all the time, the worst case; "turns": people take turns), the latency
from a window's last sample arriving to its transcript, queue depth over time, gated/dropped/failed counts,
memory and event-loop lag. The phones run in the same process as the server (they Opus-encode on the same
CPU), so numbers include that harness cost; the loop-lag column shows whether it saturated the loop.
"""
import argparse
import asyncio
import json
import re
import resource
import statistics
import subprocess
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "audio"
DEFAULT_CANDIDATES = ["mlx-community/whisper-large-v3-turbo", "mlx-community/distil-whisper-large-v3"]
SAMPLE_RATE = 16000


def load_clips() -> list[dict]:
    clips = []
    for entry in json.loads((FIXTURES / "manifest.json").read_text()):
        with wave.open(str(FIXTURES / entry["file"]), "rb") as w:
            audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
        clips.append({**entry, "audio": audio})
    return clips


def words(text: str) -> list[str]:
    return re.sub(r"[^a-z0-9' ]+", " ", text.lower()).split()


def word_errors(reference: str, hypothesis: str) -> tuple[int, int]:
    """(edit distance in words, reference word count)."""
    ref, hyp = words(reference), words(hypothesis)
    row = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        prev, row[0] = row[0], i
        for j, h in enumerate(hyp, 1):
            prev, row[j] = row[j], min(row[j] + 1, row[j - 1] + 1, prev + (r != h))
    return row[len(hyp)], len(ref)


def measure_candidate(repo: str, window_ms: list[int]) -> dict:
    """Runs inside a subprocess: returns load time, memory and the per-window-size results for one model."""
    import mlx.core as mx
    import mlx_whisper

    clips = load_clips()
    started = time.perf_counter()
    mlx_whisper.transcribe(np.zeros(SAMPLE_RATE, dtype=np.float32), path_or_hf_repo=repo, language="en")
    load_s = time.perf_counter() - started  # first call includes loading the weights and warming up

    def transcribe(audio):
        began = time.perf_counter()
        out = mlx_whisper.transcribe(audio, path_or_hf_repo=repo, language="en", condition_on_previous_text=False,
                                     temperature=0.0)
        return out, (time.perf_counter() - began) * 1000

    results = {"repo": repo, "load_s": round(load_s, 2), "windows": {}}
    for ms in [0, *window_ms]:  # 0 means the whole clip in one call
        step = len(clips[0]["audio"]) if ms == 0 else int(SAMPLE_RATE * ms / 1000)
        errors = total = 0
        latencies = []
        sample = ""
        for clip in clips:
            audio = clip["audio"]
            pieces = [audio] if ms == 0 else [audio[i:i + step] for i in range(0, len(audio), step)
                                              if len(audio[i:i + step]) >= SAMPLE_RATE // 4]
            heard = []
            for piece in pieces:
                out, latency = transcribe(piece)
                latencies.append(latency)
                heard.append(out["text"].strip())
            text = " ".join(t for t in heard if t)
            e, n = word_errors(clip["text"], text)
            errors, total = errors + e, total + n
            if clip["file"] == "budget.wav":
                sample = text
        key = "whole clip" if ms == 0 else f"{ms} ms"
        results["windows"][key] = {
            "wer": round(errors / total, 3), "calls": len(latencies),
            "latency_ms_median": round(statistics.median(latencies)), "latency_ms_p95": round(float(np.percentile(latencies, 95))),
            "example (budget.wav)": sample,
        }
    results["peak_rss_mb"] = round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024)
    results["mlx_peak_gpu_mb"] = round(mx.get_peak_memory() / 1024 / 1024)
    return results


# --- pipeline measurement ---------------------------------------------------------------------


def speech_at_48k(offset_s: float, pause_s: float, duty: float) -> np.ndarray:
    """A looping 48 kHz int16 microphone signal built from the fixtures.

    ``duty`` is the share of the time this device speaks: 1.0 keeps a ``pause_s`` gap between clips; below 1.0 the
    clips are padded with silence so the device is active only that share of the time.
    """
    parts = []
    for clip in load_clips():
        parts += [clip["audio"], np.zeros(int(SAMPLE_RATE * pause_s), np.float32)]
    stream = np.concatenate(parts)
    active = stream[np.abs(stream) > 0.01]
    stream = stream * (10 ** ((-20 - 20 * np.log10(np.sqrt(np.mean(active ** 2)))) / 20))  # active speech at -20 dBFS
    if duty < 1.0:
        stream = np.concatenate([stream, np.zeros(int(len(stream) * (1 / duty - 1)), np.float32)])
    stream = np.roll(stream, int(offset_s * SAMPLE_RATE))
    upsampled = np.interp(np.linspace(0, len(stream) - 1, len(stream) * 3), np.arange(len(stream)), stream)
    return np.clip(upsampled * 32767, -32768, 32767).astype("<i2")


async def run_scenario(adapter, devices: int, scenario: str, seconds: float, window_ms: int, queue_max: int,
                       segmentation: str | None = None) -> dict:
    import tempfile
    from dataclasses import replace
    from datetime import datetime, timezone
    from server.config import PipelineConfig, load_settings
    from server.repositories import audit_events
    from tests.support.server import settings_in, start_server
    from tests.support.synthetic_phone import SyntheticPhone

    settings = settings_in(Path(tempfile.mkdtemp()))
    tuned = replace(load_settings().pipeline, window_ms=window_ms, queue_max=queue_max)
    if segmentation:
        tuned = replace(tuned, segmentation=segmentation)
    settings = replace(settings, pipeline=tuned)
    server = await start_server(settings, stt_adapter=adapter, stt_loaded=True)
    outcomes: list[tuple[float, object]] = []
    server.runtime.on_transcribed_window(lambda o: outcomes.append((time.time(), o)))
    phones = []
    lag, depth = [], []
    try:
        async with __import__("aiohttp").ClientSession() as http, http.post(server.base_url + "/api/meetings", json={}) as r:
            meeting_id = (await r.json())["meeting"]["meeting_id"]
        duty = 1.0 if scenario == "continuous" else 0.34
        for i in range(devices):
            offset = (i * 7.3) if scenario == "continuous" else i * (25.0 / devices)
            samples = speech_at_48k(offset, pause_s=0.3 if scenario == "continuous" else 0.5, duty=duty)
            phone = SyntheticPhone(server.base_url, meeting_id, name=f"phone{i}", samples=samples)
            phones.append(phone)
            await phone.connect()
        for phone in phones:
            await phone.wait_connected()

        async def sampler():
            last = time.monotonic()
            while True:
                await asyncio.sleep(0.05)
                now = time.monotonic()
                lag.append((now - last - 0.05) * 1000)
                last = now

        async def depth_sampler():
            while True:
                await asyncio.sleep(0.5)
                depth.append(server.runtime.pipeline.scheduler.total_backlog())

        tasks = [asyncio.create_task(sampler()), asyncio.create_task(depth_sampler())]
        start = time.time()
        await asyncio.sleep(seconds)
        for t in tasks:
            t.cancel()
        finished = await server.runtime.pipeline.drain(meeting_id, 60)
        stats = server.runtime.pipeline.scheduler.stats()
        dstats = server.runtime.pipeline.device_stats()
        drops = len(audit_events.list_events(server.runtime.db.conn, event_type="stt_window_dropped"))
    finally:
        for phone in phones:
            await phone.close()
        await server.stop()

    def parse(ts):
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()

    ok = [(when, o) for when, o in outcomes if o.status == "ok"]
    post_window = [(when - parse(o.t_end)) * 1000 for when, o in ok if when - start < seconds + 1]
    model_ms = [o.latency_ms for _, o in outcomes if o.latency_ms and o.status in ("ok", "empty")]
    queue_ms = [o.queued_ms for _, o in ok]
    per_device = {d[:4]: {k: stats[d][k] for k in ("enqueued", "transcribed", "empty", "failed", "dropped", "suppressed")}
                  | {"gated": dstats[d]["windows_gated"], "seen": dstats[d]["windows_seen"]} for d in stats}
    pct = lambda xs, q: round(float(np.percentile(xs, q))) if xs else None  # noqa: E731
    failures = sorted({o.error for _, o in outcomes if o.status == "failed"})[:2]
    return {"devices": devices, "scenario": scenario, "seconds": seconds, "windows_ok": len(ok), "failure_samples": failures,
            "post_window_latency_ms": {"median": pct(post_window, 50), "p95": pct(post_window, 95), "max": pct(post_window, 100)},
            "model_ms_median": pct(model_ms, 50), "queue_wait_ms_median": pct(queue_ms, 50), "queue_wait_ms_p95": pct(queue_ms, 95),
            "queue_depth": {"max": max(depth, default=0), "mean": round(float(np.mean(depth)), 1) if depth else 0},
            "drain_completed": finished.drained, "audit_drops": drops,
            "loop_lag_ms": {"p95": pct(lag, 95), "max": pct(lag, 100)}, "per_device": per_device}


def measure_pipeline(args) -> None:
    import resource
    import mlx.core as mx
    sys.path.insert(0, str(ROOT))
    from server.config import load_settings
    from server.pipeline.mlx_whisper_adapter import build_adapter

    settings = load_settings()
    print(f"model {settings.stt.model}; window {args.window_ms} ms, queue_max {args.queue_max}, "
          f"workers {settings.pipeline.workers}", flush=True)
    for scenario in args.scenarios:
        for devices in args.devices:
            adapter = build_adapter(settings.stt)  # the server closes its adapter on shutdown, so each run loads its own
            adapter.load()
            result = asyncio.run(run_scenario(adapter, devices, scenario, args.seconds, args.window_ms, args.queue_max,
                                              args.segmentation))
            print(json.dumps(result), flush=True)
    print(json.dumps({"peak_rss_mb": round(resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / 1024 / 1024),
                      "mlx_peak_gpu_mb": round(mx.get_peak_memory() / 1024 / 1024)}))


# --- windowing strategy comparison ------------------------------------------------------------


def build_stream(kind: str) -> tuple[np.ndarray, str]:
    """(audio at 16 kHz float32, reference text) for one realistic stream made of the fixture sentences."""
    rng = np.random.default_rng(5)
    clips = load_clips()
    if kind == "sentences":     # natural pauses between sentences
        gaps = rng.uniform(0.8, 1.6, len(clips))
    elif kind == "run-on":      # one long stretch: barely a breath between sentences
        gaps = np.full(len(clips), 0.25)
    else:                       # "noisy": sentences with pauses, over pink noise at about 12 dB SNR
        gaps = rng.uniform(0.8, 1.6, len(clips))
    parts = [np.zeros(SAMPLE_RATE, np.float32)]
    for clip, gap in zip(clips, gaps):
        parts += [clip["audio"], np.zeros(int(SAMPLE_RATE * gap), np.float32)]
    audio = np.concatenate(parts)
    audio = audio * (10 ** ((-28 - 20 * np.log10(np.sqrt(np.mean(audio[np.abs(audio) > 0.01] ** 2)))) / 20))
    if kind == "noisy":
        f = np.fft.rfft(rng.standard_normal(len(audio)))
        f /= np.sqrt(np.maximum(np.arange(len(f)), 1))
        noise = np.fft.irfft(f, len(audio))
        audio = audio + noise / noise.std() * (10 ** ((-28 - 12) / 20))
    return audio.astype(np.float32), " ".join(c["text"] for c in clips)


def measure_strategies(args) -> None:
    sys.path.insert(0, str(ROOT))
    from server.config import load_settings
    from server.pipeline.mlx_whisper_adapter import build_adapter
    from server.pipeline.pipeline import segment_config, vad_config
    from server.pipeline.segmenting import Segmenter
    from server.pipeline.vad import VadConfig
    from server.pipeline.windowing import Windower

    settings = load_settings()
    adapter = build_adapter(settings.stt)
    adapter.load()
    pc = settings.pipeline
    strategies = {
        "fixed 1 s": lambda: Windower("d", "m", SAMPLE_RATE, 1000, vad_config(pc), pc.min_speech_fraction),
        "fixed 3 s": lambda: Windower("d", "m", SAMPLE_RATE, 3000, vad_config(pc), pc.min_speech_fraction),
        "segments": lambda: Segmenter("d", "m", SAMPLE_RATE, vad_config(pc), segment_config(pc)),
    }
    print(f"{'stream':10s} {'strategy':10s} {'lines':>5s} {'model calls':>11s} {'model time':>10s} {'WER':>6s}  first lines heard")
    for kind in ("sentences", "run-on", "noisy"):
        audio, reference = build_stream(kind)
        for name, make in strategies.items():
            stage = make()
            emitted = []
            for i in range(0, len(audio) - 319, 320):
                windows, _ = stage.feed(audio[i:i + 320], 1000.0 + (i + 320) / SAMPLE_RATE)
                emitted += windows
            windows, _ = stage.flush()
            emitted += windows
            texts, model_s = [], 0.0
            for w in emitted:
                began = time.perf_counter()
                result = adapter.transcribe_window("d", w.window_id, w.audio)
                model_s += time.perf_counter() - began
                if result.text.strip():
                    texts.append(result.text.strip())
            errors, total = word_errors(reference, " ".join(texts))
            print(f"{kind:10s} {name:10s} {len(texts):5d} {len(emitted):11d} {model_s:9.1f}s {errors / total:6.3f}  "
                  f"{' | '.join(texts[:2])[:70]!r}", flush=True)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="command", required=True)
    models = sub.add_parser("models", help="compare candidate STT models")
    models.add_argument("--candidate", action="append", help="Hugging Face repo id (repeatable)")
    models.add_argument("--windows", type=int, nargs="+", default=[1000, 2000, 3000], help="window lengths in ms")
    models.add_argument("--one", help="internal: measure a single candidate and print JSON")
    pipe = sub.add_parser("pipeline", help="synthetic phones through the real server and model")
    pipe.add_argument("--devices", type=int, nargs="+", default=[1, 2, 5])
    pipe.add_argument("--scenarios", nargs="+", default=["continuous", "turns"], choices=["continuous", "turns"])
    pipe.add_argument("--seconds", type=float, default=40.0)
    pipe.add_argument("--window-ms", type=int, default=1000)
    pipe.add_argument("--queue-max", type=int, default=4)
    pipe.add_argument("--segmentation", choices=["segments", "fixed"], default=None,
                      help="override [pipeline] segmentation for this run")
    sub.add_parser("strategies", help="compare fixed windows with speech segments on realistic streams (real model)")
    args = parser.parse_args()

    if args.command == "strategies":
        measure_strategies(args)
        return
    if args.command == "pipeline":
        measure_pipeline(args)
        return

    if args.command == "models":
        if args.one:
            print("RESULT " + json.dumps(measure_candidate(args.one, args.windows)))
            return
        for repo in args.candidate or DEFAULT_CANDIDATES:
            print(f"\n=== {repo} ===", flush=True)
            proc = subprocess.run([sys.executable, __file__, "models", "--one", repo, "--windows", *map(str, args.windows)],
                                  capture_output=True, text=True)
            line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT ")), None)
            if line is None:
                print("FAILED:\n" + proc.stderr[-1500:])
                continue
            result = json.loads(line[7:])
            print(f"load {result['load_s']} s | peak RSS {result['peak_rss_mb']} MB | peak MLX GPU {result['mlx_peak_gpu_mb']} MB")
            for size, r in result["windows"].items():
                print(f"  {size:>10}: WER {r['wer']:.3f} | latency median {r['latency_ms_median']} ms, p95 {r['latency_ms_p95']} ms "
                      f"({r['calls']} calls) | {r['example (budget.wav)']!r}")


if __name__ == "__main__":
    main()
