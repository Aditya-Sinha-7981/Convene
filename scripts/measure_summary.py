"""Measure summarization cost against meeting length on the configured reasoning model (CON-10).

    .venv/bin/python scripts/measure_summary.py [--minutes 5 15 30 60 75 90]

For each length, builds a synthetic transcript at a continuous-talk rate (about 150 words per minute, one line
every 5 s, four speakers, lines drawn from the tests/fixtures/transcripts fixtures), renders the real summary
prompt, and reports prompt tokens, peak MLX memory, prompt (prefill) and generation speed, total time, and whether
the reply validated. Each length runs in its own subprocess so peak memory is not carried over. Weights must be
cached; nothing is downloaded. The content repeats, so this measures cost, not summary quality.
"""
import argparse
import json
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

WORDS_PER_MINUTE = 150
LINE_EVERY_S = 5


def transcript(minutes: float):
    from server.summary.prompt import TranscriptInput
    pool = []
    for path in sorted((ROOT / "tests" / "fixtures" / "transcripts").glob("*.json")):
        pool += [text for _, _, text in json.loads(path.read_text())["lines"]]
    people = ["Priya", "Sam", "Marcus", "Lena"]
    words_per_line = WORDS_PER_MINUTE * LINE_EVERY_S // 60
    words = " ".join(pool).split()
    lines, cursor = [], 0
    for index in range(int(minutes * 60 // LINE_EVERY_S)):
        chunk = [words[(cursor + k) % len(words)] for k in range(words_per_line)]
        cursor += words_per_line
        second = index * LINE_EVERY_S
        lines.append(f"[{second // 3600:02d}:{second % 3600 // 60:02d}:{second % 60:02d}] "
                     f"{people[index % len(people)]}: {' '.join(chunk)}")
    return TranscriptInput(tuple(lines), tuple(people), 0, len(lines))


def measure(minutes: float) -> dict:
    import mlx.core as mx
    from mlx_lm import stream_generate
    from mlx_lm.sample_utils import make_sampler
    from server.config import load_settings
    from server.rag.reasoning import MlxLmAdapter
    from server.summary.prompt import build_messages
    from server.summary.schema import InvalidOutput, parse_output
    settings = load_settings()
    adapter = MlxLmAdapter(settings.reasoning)
    adapter.load()
    loaded_gb = mx.get_peak_memory() / 1e9
    mx.reset_peak_memory()
    messages = build_messages(transcript(minutes))
    prompt = adapter._tokenizer.apply_chat_template(messages, add_generation_prompt=True)
    began = time.monotonic()
    parts, last = [], None
    for last in stream_generate(adapter._model, adapter._tokenizer, prompt, max_tokens=settings.summary.max_output_tokens,
                                sampler=make_sampler(temp=settings.summary.temperature)):
        parts.append(last.text)
    total_s = time.monotonic() - began
    try:
        output = parse_output("".join(parts))
        valid, items = True, len(output.action_items)
    except InvalidOutput as exc:
        valid, items = str(exc), None
    return {"minutes": minutes, "prompt_tokens": len(prompt), "tokens_per_minute": round(len(prompt) / minutes),
            "prefill_tps": round(last.prompt_tps), "generation_tps": round(last.generation_tps, 1),
            "generated": last.generation_tokens, "finish": last.finish_reason, "total_s": round(total_s, 1),
            "prefill_s": round(len(prompt) / last.prompt_tps, 1), "weights_gb": round(loaded_gb, 2),
            "peak_mlx_gb_during_summary": round(mx.get_peak_memory() / 1e9, 2), "valid": valid, "action_items": items}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--minutes", type=float, nargs="+", default=[5, 15, 30, 60, 75, 90])
    parser.add_argument("--one", type=float, help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.one is not None:
        print("RESULT " + json.dumps(measure(args.one)), flush=True)
        return 0
    for minutes in args.minutes:
        proc = subprocess.run([sys.executable, __file__, "--one", str(minutes)], capture_output=True, text=True)
        result = next((line[7:] for line in proc.stdout.splitlines() if line.startswith("RESULT ")), None)
        print(result or f"{minutes} min: FAILED\n{proc.stderr[-2000:]}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
