# Models & Resource Configuration

## Principle

Application code never references a model name directly (ADR-14). Every capability that needs a model requests a **resource type**; this file is authoritative for which concrete model currently backs each resource type, and can be edited without touching any other code.

## Resource types

| Resource type | Purpose | Default (local) | Fallback (cloud, manual only — ADR-06) |
|---|---|---|---|
| `stt` | Transcribe an audio window | `mlx-whisper` with `mlx-community/whisper-large-v3-turbo`, **pinned to revision `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb`** in `config/convene.toml` (measured on the reference laptop, below) | Groq Whisper-large-v3 (free tier), not wired |
| `embedding` | Embed transcript chunks and questions for RAG | `BAAI/bge-small-en-v1.5` via `sentence-transformers`, CPU, pinned to revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` in `config/convene.toml` | none needed — cheap enough to always run local |
| `speaker_embedding` | Enrollment + runtime classification for shared devices | local speaker-embedding model (ECAPA-TDNN-class, CPU) | none — must run local, no meaningful cloud equivalent for this use case |
| `reasoning` | Summarization + RAG answer generation | local LLM via `mlx-lm`, 7–8B instruct class, 4-bit quantized | Groq (Llama 3.3 70B, free tier) or Gemini Flash (free tier) |

## Target hardware

MacBook Pro, M4 Pro, 24GB unified memory. All budgeting below assumes this machine; re-verify if the demo machine changes.

## Why `mlx-whisper` over `whisper.cpp`

Both are strong Apple Silicon options, but `mlx-whisper` (built directly on Apple's MLX framework) measurably outperforms `whisper.cpp`'s Metal backend on the same model in independent benchmarking — the difference is large enough to matter given multiple concurrent device streams sharing one machine's compute. `whisper.cpp` remains a documented fallback if an `mlx-whisper` environment issue ever blocks the build close to demo day (ADR-14's resource-type indirection makes that swap a config change, not a rewrite).

## STT model selection (measured, CON-05)

Measured on the reference laptop (MacBook Pro, M4 Pro, 24 GB) with `scripts/measure_stt.py models`, on the synthetic speech in `tests/fixtures/audio` (eight short sentences, two macOS voices, no digits in the references). Word error rate (WER) is on clean text-to-speech and says how models and window sizes compare, **not** how accurate any of them is on real phone audio.

| Candidate | Load (cached) | Peak RSS | Peak MLX GPU | Latency per call | WER whole clip | WER 3 s windows | WER 2 s | WER 1 s |
|---|---|---|---|---|---|---|---|---|
| `whisper-large-v3-turbo` (**pinned**) | 1.3 s | 1.77 GB | 2.36 GB | 490–525 ms | 0.000 | 0.000 | 0.062 | 0.094 |
| `distil-whisper-large-v3` | 1.4 s | 1.96 GB | 2.26 GB | 508–548 ms | 0.000 | 0.000 | 0.062 | 0.094 |
| `whisper-small-mlx` (option, not pinned) | 129 s (first, incl. download) | 1.30 GB | 1.38 GB | 143–239 ms | 0.000 | 0.047 | not run | 0.125 |
| `whisper-base-mlx` (option, not pinned) | 51 s (first, incl. download) | 0.55 GB | 0.52 GB | 37–46 ms | 0.000 | 0.031 | not run | 0.109 |

- **Turbo is pinned.** It is the documented default, and `distil-large-v3`, which the earlier version of this document held in reserve for when turbo "can't keep up", is neither faster nor more accurate: both are bound by Whisper's fixed 30 s encoder pass, so latency per call is about the same for 1 s of audio as for 3 s.
- **Capacity is about 2 windows per second per worker** for turbo (1.94 with one worker, 2.03 with two, 2.05 with four: inference is GPU-bound, so more workers only lengthen each call). At ~1 s windows that carries about two phones talking continuously, not five (`stt-pipeline.md`, `logs/stt.md`). The default pipeline now sends one call per stretch of speech (up to 8 s) rather than per second, which fits five phones talking continuously with zero drops in the synthetic measurement (`logs/stt.md`, ADR-19).
- **Smaller models are faster and less accurate**: `small` is about 5 windows/s and `base` about 25 windows/s on this machine. They are listed only as options; nothing here shows how they degrade on real, noisy, accented phone audio, and the choice is the project lead's.
- Swapping is a configuration change: edit `[models.stt]`, run `scripts/provision_models.py`, restart. `whisper.cpp` remains the documented fallback if an `mlx-whisper` environment problem ever blocks the build; it is not wired.
- The runtime pulls `torch`, `numba`, `scipy` and `tiktoken` in as dependencies of `mlx-whisper` (the virtual environment grew to about 1.1 GB); all of it works offline once installed.

## Approximate concurrent memory budget

| Component | Footprint | When resident |
|---|---|---|
| `stt` model (`whisper-large-v3-turbo`) | **measured: 1.8 GB resident (peak), 2.4 GB peak GPU** | continuously, for the whole meeting |
| `reasoning` model (7–8B, 4-bit) | ~4–6GB | only during summarization/QA calls, not continuously |
| `embedding` model | ~150–300MB | on new chunk creation, cheap and constant |
| `speaker_embedding` model | ~200–500MB | only for shared-device windows |
| **Worst case, everything loaded at once** | **~7–9GB (estimate; only the `stt` row above is measured)** | leaves 15GB+ headroom on a 24GB machine |

This headroom is the margin for "several phones talking at once plus a live Q&A call in flight" without swapping or stalling. If real testing shows this budget is wrong, correct this table — it is a claim to be verified, not assumed. The `reasoning`, `embedding` and `speaker_embedding` rows are still unmeasured estimates (CON-08, CON-09, CON-13). The initial CON-08 embedding configuration is `BAAI/bge-small-en-v1.5` (384 dimensions, 512-token input), normalized vectors with cosine distance. Its real-model latency, memory, and fixture retrieval quality must be measured and recorded in `logs/rag.md` before calling the choice validated.

## Scheduling priority across resource types

STT has scheduling priority over `reasoning` calls (see `stt-pipeline.md`) — a live transcript stalling is a worse demo failure than a summarization or Q&A answer taking an extra second or two. This is a scheduling policy, not a resource-type configuration, and lives in the STT pipeline's worker pool logic. It is implemented as a cooperative gate (`server/pipeline/priority.py`): reasoning and embedding callers `await` it before starting and wait while the STT backlog is high, up to a maximum wait (`priority_max_wait_s`). MLX inference cannot be preempted, so a job already running is not interrupted; how much this interferes is measured in CON-15.

## Cloud fallback activation

Cloud resource-type backends exist behind the exact same adapter interface as their local counterparts (`transcribe_window(...)`, `generate(...)`) and are selected purely by configuration — never invoked automatically from within the live transcription or Q&A critical path (ADR-06). Activating one is a deliberate action (a config flag or an explicit "use cloud" toggle in the dashboard), logged as such, not a silent runtime failover — a silent failover risks masking a real local-performance problem that should instead be fixed or tuned before demo day.

## Model swap procedure

Because every reference to a model is indirect (ADR-14): change the resource-type mapping in this document (and the corresponding config value in code), restart the relevant adapter, done. No application code in `stt-pipeline.md`, `rag-and-qa.md`, or `summarization.md` should ever need to change for a model swap.
