"""Measure local reasoning-model candidates (CON-09) without allowing downloads.

    .venv/bin/python scripts/measure_reasoning.py [--candidate REPO@REVISION ...]

Each candidate runs in its own subprocess so memory is clean: cached load time (including the warm-up), peak
MLX memory, peak RSS, and prompt/generation speed on a Q&A-sized prompt (five transcript excerpts, the real
system prompt) answered three times. Default: the configured model. Weights must already be cached
(`scripts/provision_models.py --resource reasoning --model REPO`).
"""
import argparse
import json
import resource
import subprocess
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import load_settings  # noqa: E402


def measure(model: str, revision: str) -> dict:
    import mlx.core as mx
    from mlx_lm import stream_generate
    from mlx_lm.sample_utils import make_sampler
    from server.rag.qa import build_messages
    from server.rag.reasoning import MlxLmAdapter
    adapter = MlxLmAdapter(replace(load_settings().reasoning, model=model, revision=revision))
    adapter.load()
    fixture = json.loads((ROOT / "tests" / "fixtures" / "qa" / "product_sync.json").read_text())
    lines = [f"[{speaker}, 00:{second // 60:02d}:{second % 60:02d}] {text}" for speaker, second, text in fixture["lines"]]
    excerpts = ["\n".join(lines[index:index + 6]) for index in range(0, 30, 6)]
    messages = build_messages("What did we decide about the beta launch date, and who owns the go or no-go call?",
                              excerpts)
    prompt = adapter._tokenizer.apply_chat_template(messages, add_generation_prompt=True)
    runs = []
    for _ in range(3):
        last = None
        for last in stream_generate(adapter._model, adapter._tokenizer, prompt, max_tokens=200,
                                    sampler=make_sampler(temp=0.0)):
            pass
        runs.append({"prompt_tps": round(last.prompt_tps), "generation_tps": round(last.generation_tps, 1),
                     "generated": last.generation_tokens})
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 ** 3)  # bytes on macOS
    return {"model": model, "revision": revision, "load_s": round(adapter.load_seconds, 2),
            "prompt_tokens": len(prompt), "runs": runs, "mlx_peak_gb": round(mx.get_peak_memory() / 1e9, 2),
            "peak_rss_gb": round(rss, 2), "context_length": getattr(adapter._model, "args", None)
            and getattr(adapter._model.args, "max_position_embeddings", None)}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidate", action="append", help="REPO@REVISION (repeatable)")
    parser.add_argument("--one", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.one:
        model, _, revision = args.one.partition("@")
        print("RESULT " + json.dumps(measure(model, revision)))
        return 0
    config = load_settings().reasoning
    status = 0
    for candidate in args.candidate or [f"{config.model}@{config.revision}"]:
        proc = subprocess.run([sys.executable, __file__, "--one", candidate], capture_output=True, text=True)
        lines = [line for line in proc.stdout.splitlines() if line.startswith("RESULT ")]
        if proc.returncode or not lines:
            print(f"{candidate}: FAILED\n{proc.stderr[-2000:]}")
            status = 1
            continue
        print(lines[-1][len("RESULT "):], flush=True)
    return status


if __name__ == "__main__":
    raise SystemExit(main())
