"""Download and verify the STT model weights. Run once, while online, before the demo.

    .venv/bin/python scripts/provision_models.py                 # download the model pinned in config/convene.toml
    .venv/bin/python scripts/provision_models.py --model REPO    # download REPO at its latest revision and print the pin
    .venv/bin/python scripts/provision_models.py --check         # verify only; needs no network

The server never downloads models. This script is the only place that does. After downloading it verifies the
weights the way the server will use them: resolved from the local cache with networking off, loaded, and used
to transcribe a fixture.
"""
import argparse
import sys
import time
import wave
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server.config import SttModelConfig, load_settings  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", help="repository id to provision (default: [models.stt] model)")
    parser.add_argument("--revision", help="exact revision (default: [models.stt] revision, else the latest)")
    parser.add_argument("--check", action="store_true", help="verify the cached weights only; never touches the network")
    args = parser.parse_args()

    config = load_settings().stt
    model = args.model or config.model
    revision = args.revision or (config.revision if not args.model else "")
    if not model:
        print("No model given and none pinned in [models.stt]. Pass --model REPO.", file=sys.stderr)
        return 2

    if not args.check:
        from huggingface_hub import HfApi, snapshot_download
        revision = revision or HfApi().model_info(model).sha
        print(f"Downloading {model} at revision {revision} ...", flush=True)
        snapshot_download(repo_id=model, revision=revision)

    from dataclasses import replace
    from server.pipeline.adapter import ModelNotProvisionedError
    from server.pipeline.mlx_whisper_adapter import MlxWhisperAdapter
    adapter = MlxWhisperAdapter(replace(config, model=model, revision=revision))
    try:
        adapter.load()
    except ModelNotProvisionedError as exc:
        print(f"NOT PROVISIONED: {exc}", file=sys.stderr)
        return 1
    clip = ROOT / "tests" / "fixtures" / "audio" / "ship_beta.wav"
    with wave.open(str(clip), "rb") as w:
        audio = np.frombuffer(w.readframes(w.getnframes()), dtype="<i2").astype(np.float32) / 32768.0
    began = time.perf_counter()
    result = adapter.transcribe_window("provision", 1, audio)
    print(f"Verified offline: loaded in {adapter.load_seconds:.1f} s; {clip.name} -> {result.text!r} "
          f"(stt_confidence {result.stt_confidence:.2f}, {(time.perf_counter() - began) * 1000:.0f} ms)")
    print("\nPin it in config/convene.toml:\n\n[models.stt]\nruntime = \"mlx\"\n"
          f"model = \"{model}\"\nrevision = \"{revision}\"")
    return 0


if __name__ == "__main__":
    sys.exit(main())
