# Models & Resource Configuration

## Principle

Application code never references a model name directly (ADR-14). Every capability that needs a model requests a **resource type**; this file is authoritative for which concrete model currently backs each resource type, and can be edited without touching any other code.

## Resource types

| Resource type | Purpose | Default (local) | Fallback (cloud, manual only — ADR-06) |
|---|---|---|---|
| `stt` | Transcribe an audio window | `mlx-whisper`, `whisper-large-v3-turbo` (or `distil-large-v3` if load-testing shows turbo can't keep up at target device count) | Groq Whisper-large-v3 (free tier) |
| `embedding` | Embed transcript chunks and questions for RAG | local sentence-embedding model (`bge-small`-class, CPU) | none needed — cheap enough to always run local |
| `speaker_embedding` | Enrollment + runtime classification for shared devices | local speaker-embedding model (ECAPA-TDNN-class, CPU) | none — must run local, no meaningful cloud equivalent for this use case |
| `reasoning` | Summarization + RAG answer generation | local LLM via `mlx-lm`, 7–8B instruct class, 4-bit quantized | Groq (Llama 3.3 70B, free tier) or Gemini Flash (free tier) |

## Target hardware

MacBook Pro, M4 Pro, 24GB unified memory. All budgeting below assumes this machine; re-verify if the demo machine changes.

## Why `mlx-whisper` over `whisper.cpp`

Both are strong Apple Silicon options, but `mlx-whisper` (built directly on Apple's MLX framework) measurably outperforms `whisper.cpp`'s Metal backend on the same model in independent benchmarking — the difference is large enough to matter given multiple concurrent device streams sharing one machine's compute. `whisper.cpp` remains a documented fallback if an `mlx-whisper` environment issue ever blocks the build close to demo day (ADR-14's resource-type indirection makes that swap a config change, not a rewrite).

## Approximate concurrent memory budget

| Component | Footprint | When resident |
|---|---|---|
| `stt` model (turbo/medium-class) | ~1.5–2GB | continuously, for the whole meeting |
| `reasoning` model (7–8B, 4-bit) | ~4–6GB | only during summarization/QA calls, not continuously |
| `embedding` model | ~150–300MB | on new chunk creation, cheap and constant |
| `speaker_embedding` model | ~200–500MB | only for shared-device windows |
| **Worst case, everything loaded at once** | **~7–9GB** | leaves 15GB+ headroom on a 24GB machine |

This headroom is the margin for "several phones talking at once plus a live Q&A call in flight" without swapping or stalling. If real testing shows this budget is wrong, correct this table — it is a claim to be verified, not assumed.

## Scheduling priority across resource types

STT has scheduling priority over `reasoning` calls (see `stt-pipeline.md`) — a live transcript stalling is a worse demo failure than a summarization or Q&A answer taking an extra second or two. This is a scheduling policy, not a resource-type configuration, and lives in the STT pipeline's worker pool logic.

## Cloud fallback activation

Cloud resource-type backends exist behind the exact same adapter interface as their local counterparts (`transcribe_window(...)`, `generate(...)`) and are selected purely by configuration — never invoked automatically from within the live transcription or Q&A critical path (ADR-06). Activating one is a deliberate action (a config flag or an explicit "use cloud" toggle in the dashboard), logged as such, not a silent runtime failover — a silent failover risks masking a real local-performance problem that should instead be fixed or tuned before demo day.

## Model swap procedure

Because every reference to a model is indirect (ADR-14): change the resource-type mapping in this document (and the corresponding config value in code), restart the relevant adapter, done. No application code in `stt-pipeline.md`, `rag-and-qa.md`, or `summarization.md` should ever need to change for a model swap.
