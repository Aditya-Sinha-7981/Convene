"""Measure local embedding candidates without allowing model downloads (CON-08).

    .venv/bin/python scripts/measure_embeddings.py [--candidate REPO[@REVISION] ...]

Each candidate runs in its own subprocess so peak memory is clean. Reported: cached load time, peak RSS,
latency for 32 chunk-sized inputs, vector dimension, the model's maximum input length, and top-1 accuracy on a
small fixture transcript (each question should rank its own chunk first). Weights must already be cached
(`scripts/provision_models.py --resource embedding --model REPO`). Default: the configured model only.
"""
import argparse
import json
import os
import resource
import subprocess
import sys
import time
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import load_settings  # noqa: E402

# A synthetic meeting: chunk text in the stored format, and one question per chunk that paraphrases it.
FIXTURE = [
    ("[Asha, 00:00:05] The budget for the pilot is forty thousand rupees, and finance signed off yesterday.",
     "How much money do we have for the pilot?"),
    ("[Bharat, 00:01:10] The design review moved to Tuesday afternoon because the client is travelling.",
     "When is the design review now?"),
    ("[Chitra, 00:02:30] I will own the Android crash fix and send a build to QA by Thursday.",
     "Who is fixing the Android crash?"),
    ("[Asha, 00:03:45] We decided to drop the offline map feature from this release to reduce risk.",
     "Which feature was cut from the release?"),
    ("[Speaker on Phone 3, 00:05:00] The hotel for the offsite is booked near the airport for three nights.",
     "Where are we staying for the offsite?"),
    ("[Bharat, 00:06:20] Latency on the login API went from two hundred to nine hundred milliseconds after the deploy.",
     "Did the deploy make anything slower?"),
    ("[Chitra, 00:07:40] The new intern starts on Monday and will shadow the support team for a week.",
     "When does the new hire begin?"),
    ("[Asha, 00:09:00] Customer churn dropped to three percent after we added the onboarding emails.",
     "What happened to churn?"),
]


def measure(model: str, revision: str) -> dict:
    """Load a candidate exactly as SentenceTransformerAdapter does (cache only, CPU, normalized vectors)."""
    os.environ["HF_HUB_OFFLINE"] = "1"
    from sentence_transformers import SentenceTransformer
    began = time.perf_counter()
    st = SentenceTransformer(model, revision=revision, local_files_only=True, device="cpu")
    load_ms = (time.perf_counter() - began) * 1000

    def embed(texts):
        return list(st.encode(list(texts), normalize_embeddings=True, convert_to_numpy=True, show_progress_bar=False))
    texts = [FIXTURE[i % len(FIXTURE)][0] * 4 for i in range(32)]  # ~chunk-sized inputs
    embed(texts[:2])  # warm-up
    began = time.perf_counter()
    vectors = embed(texts)
    embed_ms = (time.perf_counter() - began) * 1000
    chunks = embed([chunk for chunk, _ in FIXTURE])
    questions = embed([question for _, question in FIXTURE])
    ranks = [int(np.argmax([float(np.dot(q, c)) for c in chunks])) for q in questions]
    hits = sum(rank == index for index, rank in enumerate(ranks))
    # macOS ru_maxrss is bytes (Linux reports KiB); this script runs on the macOS reference laptop.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    return {"model": model, "revision": revision, "dimension": len(vectors[0]),
            "max_input_tokens": st.get_max_seq_length(), "load_ms": round(load_ms, 1),
            "embed_32_ms": round(embed_ms, 1), "per_chunk_ms": round(embed_ms / len(texts), 2),
            "peak_rss_mib": round(rss, 1), "fixture_top1": f"{hits}/{len(FIXTURE)}",
            "misses": [FIXTURE[i][1] for i, rank in enumerate(ranks) if rank != i]}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--candidate", action="append", help="REPO or REPO@REVISION (repeatable)")
    parser.add_argument("--one", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.one:
        model, _, revision = args.one.partition("@")
        print("RESULT " + json.dumps(measure(model, revision or "main")))
        return 0
    config = load_settings().embedding
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
