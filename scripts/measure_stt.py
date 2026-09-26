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
With `--embedding`, the CON-08 transcript indexer runs with the real local embedding model, so the STT numbers
can be compared with and without indexing, and each result adds the indexing lag: from an utterance being
written to the first time a `ready` chunk covers it (sampled every 100 ms). With `--qa` (implies `--embedding`),
the reasoning model is loaded too and a question is asked every `--qa-every` seconds while the phones talk; each
result adds the question-to-answer latency and outcomes, so STT latency can be compared with Q&A running.
With `--summarize-at S`, the reasoning model is loaded and one manual summary of the live meeting is started S
seconds in (CON-10); each result adds its time to summary and the STT latency of windows that ended while it
ran, next to the latency of windows well outside it.
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
                       segmentation: str | None = None, embedding_adapter=None, reasoning_adapter=None,
                       qa_every_s: float = 8.0, summarize_at_s: float | None = None) -> dict:
    import tempfile
    from dataclasses import replace
    from datetime import datetime, timezone
    from server.config import PipelineConfig, load_settings
    from server.repositories import audit_events
    from tests.support.server import settings_in, start_server
    from tests.support.synthetic_phone import SyntheticPhone

    def parse(ts):
        return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%fZ").replace(tzinfo=timezone.utc).timestamp()

    settings = settings_in(Path(tempfile.mkdtemp()))
    tuned = replace(load_settings().pipeline, window_ms=window_ms, queue_max=queue_max)
    if segmentation:
        tuned = replace(tuned, segmentation=segmentation)
    settings = replace(settings, pipeline=tuned)
    server = await start_server(settings, stt_adapter=adapter, stt_loaded=True, embedding_adapter=embedding_adapter,
                                reasoning_adapter=reasoning_adapter)
    outcomes: list[tuple[float, object]] = []
    server.runtime.on_transcribed_window(lambda o: outcomes.append((time.time(), o)))
    phones = []
    lag, depth = [], []
    searchable: dict[str, float] = {}  # utterance_id -> seconds from written to covered by a ready chunk
    answers: list[tuple[float, str, str | None]] = []  # (seconds, status, reason) per question
    summary_run: dict = {}  # CON-10: one manual "summarize now" during live speech
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

        async def index_sampler():
            from server.repositories import transcript_chunks, utterances

            def covered(tx):
                rows = utterances.list_for_meeting(tx.conn, meeting_id)
                position = {row.utterance_id: i for i, row in enumerate(rows)}
                done = set()
                for chunk in transcript_chunks.list_for_meeting(tx.conn, meeting_id):
                    if chunk.status == "ready":
                        done.update(r.utterance_id for r in rows[position[chunk.utterance_id_start]:
                                                                 position[chunk.utterance_id_end] + 1])
                return {row.utterance_id: row.created_at for row in rows if row.utterance_id in done}
            while True:
                await asyncio.sleep(0.1)
                now = time.time()
                for utterance_id, created in (await server.runtime.db.run(covered)).items():
                    searchable.setdefault(utterance_id, now - parse(created))

        async def asker():
            questions = ["What did we decide about shipping the beta?", "What is the budget?",
                         "Who is presenting at the board meeting?"]
            number = 0
            while True:
                await asyncio.sleep(qa_every_s)
                began = time.monotonic()
                result = await server.runtime.qa.ask(meeting_id, {"question": questions[number % len(questions)]})
                answers.append((time.monotonic() - began, result["query"]["status"], result["reason"]))
                number += 1

        async def summarizer():
            from server.repositories import summaries
            await asyncio.sleep(summarize_at_s)
            summary_run["start"] = time.time()
            started = await server.runtime.summary.summarize(meeting_id)
            while server.runtime.summary.running(meeting_id):
                await asyncio.sleep(0.05)
            summary_run["end"] = time.time()
            row = await server.runtime.db.run(lambda tx: summaries.get(tx.conn, started["summary_id"]))
            summary_run["status"] = row.status

        tasks = [asyncio.create_task(sampler()), asyncio.create_task(depth_sampler())]
        if summarize_at_s is not None:
            tasks.append(asyncio.create_task(summarizer()))
        if embedding_adapter is not None:
            tasks.append(asyncio.create_task(index_sampler()))
        if reasoning_adapter is not None and qa_every_s > 0:
            tasks.append(asyncio.create_task(asker()))
        start = time.time()
        await asyncio.sleep(seconds)
        while summarize_at_s is not None and "end" not in summary_run and time.time() - start < seconds + 300:
            await asyncio.sleep(0.1)  # let a summary that started near the end finish (speech keeps coming)
        for t in tasks:
            t.cancel()
        finished = await server.runtime.pipeline.drain(meeting_id, 60)
        if embedding_adapter is not None:
            index_status = await server.runtime.indexer.index_status(meeting_id)
        stats = server.runtime.pipeline.scheduler.stats()
        dstats = server.runtime.pipeline.device_stats()
        drops = len(audit_events.list_events(server.runtime.db.conn, event_type="stt_window_dropped"))
    finally:
        for phone in phones:
            await phone.close()
        await server.stop()

    ok = [(when, o) for when, o in outcomes if o.status == "ok"]
    post_window = [(when - parse(o.t_end)) * 1000 for when, o in ok if when - start < seconds + 1]
    model_ms = [o.latency_ms for _, o in outcomes if o.latency_ms and o.status in ("ok", "empty")]
    queue_ms = [o.queued_ms for _, o in ok]
    per_device = {d[:4]: {k: stats[d][k] for k in ("enqueued", "transcribed", "empty", "failed", "dropped", "suppressed")}
                  | {"gated": dstats[d]["windows_gated"], "seen": dstats[d]["windows_seen"]} for d in stats}
    pct = lambda xs, q: round(float(np.percentile(xs, q))) if xs else None  # noqa: E731
    failures = sorted({o.error for _, o in outcomes if o.status == "failed"})[:2]
    indexing = {}
    if embedding_adapter is not None:
        lags = list(searchable.values())
        indexing = {"indexing_lag_s": {"n": len(lags), "median": round(float(np.percentile(lags, 50)), 2) if lags else None,
                                       "p95": round(float(np.percentile(lags, 95)), 2) if lags else None,
                                       "max": round(max(lags), 2) if lags else None},
                    "index_status_at_end": {k: index_status[k] for k in ("ready", "failed", "indexed", "pending_utterances")}}
    if reasoning_adapter is not None and qa_every_s > 0:
        latency = [seconds_taken for seconds_taken, _, _ in answers]
        indexing["qa"] = {"asked": len(answers), "answer_s": {"median": round(float(np.median(latency)), 2) if latency else None,
                                                             "max": round(max(latency), 2) if latency else None},
                          "outcomes": sorted({f"{status}/{reason}" for _, status, reason in answers})}
    if summary_run:
        during = [(when - parse(o.t_end)) * 1000 for when, o in ok
                  if "end" in summary_run and summary_run["start"] <= parse(o.t_end) <= summary_run["end"]]
        outside = [(when - parse(o.t_end)) * 1000 for when, o in ok
                   if "end" in summary_run and not summary_run["start"] - 1 <= parse(o.t_end) <= summary_run["end"] + 5]
        indexing["summary"] = {"status": summary_run.get("status"),
                               "time_to_summary_s": round(summary_run["end"] - summary_run["start"], 1)
                               if "end" in summary_run else None,
                               "stt_post_window_ms_during": {"n": len(during), "median": pct(during, 50),
                                                             "p95": pct(during, 95), "max": pct(during, 100)},
                               "stt_post_window_ms_outside": {"n": len(outside), "median": pct(outside, 50),
                                                              "p95": pct(outside, 95)}}
    return {"devices": devices, "scenario": scenario, "seconds": seconds, "windows_ok": len(ok), "failure_samples": failures,
            **indexing,
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
          f"workers {settings.pipeline.workers}; embedding indexer {'on' if args.embedding or args.qa else 'off'}; "
          f"Q&A {'every %.0f s' % args.qa_every if args.qa else 'off'}", flush=True)
    embedding = reasoning = None
    if args.qa or args.summarize_at is not None:
        from server.rag.reasoning import build_reasoning_adapter
        reasoning = build_reasoning_adapter(settings.reasoning)
        reasoning.load()
    if args.embedding or args.qa:
        from server.rag.embedding import build_embedding_adapter
        embedding = build_embedding_adapter(settings.embedding)
        embedding.load()
    for scenario in args.scenarios:
        for devices in args.devices:
            adapter = build_adapter(settings.stt)  # the server closes its adapter on shutdown, so each run loads its own
            adapter.load()
            result = asyncio.run(run_scenario(adapter, devices, scenario, args.seconds, args.window_ms, args.queue_max,
                                              args.segmentation, embedding, reasoning,
                                              args.qa_every if args.qa else 0, args.summarize_at))
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
    pipe.add_argument("--embedding", action="store_true", help="also run the CON-08 indexer with the real embedding model")
    pipe.add_argument("--qa", action="store_true", help="also load the reasoning model and ask questions (CON-09)")
    pipe.add_argument("--qa-every", type=float, default=8.0, help="seconds between questions with --qa")
    pipe.add_argument("--summarize-at", type=float, help="load the reasoning model and trigger one manual summary this "
                                                           "many seconds in, measuring STT latency while it runs (CON-10)")
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
