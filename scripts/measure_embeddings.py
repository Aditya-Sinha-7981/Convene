"""Measure the provisioned local embedding adapter without allowing model downloads."""
import os
import sys
import time
import resource
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import load_settings  # noqa: E402
from server.rag.embedding import build_embedding_adapter  # noqa: E402


def main():
    os.environ["HF_HUB_OFFLINE"] = "1"
    adapter = build_embedding_adapter(load_settings().embedding)
    began = time.perf_counter()
    adapter.load()
    load_ms = (time.perf_counter() - began) * 1000
    texts = ["[Asha, 00:00:01] The budget is forty thousand rupees."] * 32
    began = time.perf_counter()
    vectors = adapter.embed(texts)
    embed_ms = (time.perf_counter() - began) * 1000
    # macOS ru_maxrss is bytes; Linux reports KiB. This task runs on macOS, label it explicitly.
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)
    print(f"model={adapter.model_identifier} dimension={len(vectors[0])} load_ms={load_ms:.1f} "
          f"embed_32_ms={embed_ms:.1f} per_chunk_ms={embed_ms / len(texts):.1f} peak_rss_mib_macos={rss:.2f}")


if __name__ == "__main__":
    main()
