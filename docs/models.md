# Models & Resource Configuration

## Principle

Application code never references a model name directly (ADR-14). Every capability that needs a model requests a **resource type**; this file is authoritative for which concrete model currently backs each resource type, and can be edited without touching any other code.

## Resource types

| Resource type | Purpose | Default (local) | Fallback (cloud, manual only — ADR-06) |
|---|---|---|---|
| `stt` | Transcribe an audio window | `mlx-whisper` with `mlx-community/whisper-large-v3-turbo`, **pinned to revision `a4aaeec0636e6fef84abdcbe3544cb2bf7e9f6fb`** in `config/convene.toml` (measured on the reference laptop, below) | Groq Whisper-large-v3 (free tier), not wired |
| `embedding` | Embed transcript chunks and questions for RAG | `BAAI/bge-small-en-v1.5` via `sentence-transformers`, CPU, pinned to revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a` in `config/convene.toml` | none needed — cheap enough to always run local |
| `speaker_embedding` | Enrollment + runtime classification for shared devices | local speaker-embedding model (ECAPA-TDNN-class, CPU) | none — must run local, no meaningful cloud equivalent for this use case |
| `reasoning` | Summarization + RAG answer generation | `mlx-lm` with `mlx-community/Meta-Llama-3.1-8B-Instruct-4bit`, **pinned to revision `241a666dad6cb93c8ff213d39a7f34a36bf26db4`**, resident from startup (measured, below) | Groq (Llama 3.3 70B, free tier) or Gemini Flash (free tier), not wired |

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
| `reasoning` model (`Llama-3.1-8B-Instruct-4bit`) | **measured: 5.5 GB peak MLX memory alone** | resident for the whole meeting (CON-09: no cold load at the first question) |
| `embedding` model (`bge-small-en-v1.5`) | **measured: about 0.4 GB added to the server's peak RSS** | continuously (loaded at startup), used after each settle window |
| `speaker_embedding` model | ~200–500MB | only for shared-device windows |
| **STT + embedding + reasoning loaded, 5 phones talking, questions every 8 s** | **measured: 5.7 GB peak RSS, 7.1 GB peak MLX memory** | about 15 GB headroom on 24 GB; `speaker_embedding` (CON-13) is not yet measured |
| `reasoning` during a summary (CON-10) | **measured: 5.5 GB peak MLX at 2k prompt tokens, 7.1 GB at the 16k limit, 8.7 GB at 27k** | the key-value cache grows with the transcript; `[summary].max_input_tokens` caps it (`summarization.md`, "Long transcripts") |

This headroom is the margin for "several phones talking at once plus a live Q&A call in flight" without swapping or stalling. If real testing shows this budget is wrong, correct this table — it is a claim to be verified, not assumed. The `reasoning`, `embedding` and `speaker_embedding` rows are still unmeasured estimates (CON-08, CON-09, CON-13). The `embedding` (CON-08) and `reasoning` (CON-09) rows are measured below; `speaker_embedding` remains an estimate.

## Embedding model selection (measured, CON-08)

Measured on the reference laptop with `scripts/measure_embeddings.py` (each candidate in its own process, weights cached, networking off in the Hugging Face client). Latency is for 32 inputs of about chunk size; the fixture is eight synthetic transcript chunks, each with a paraphrased question that should rank it first.

| Candidate | Dimension | Max input | Load (cached, excl. import) | Per chunk | Peak RSS | Fixture top-1 |
|---|---|---|---|---|---|---|
| `BAAI/bge-small-en-v1.5` (**pinned**) | 384 | 512 | 58 ms | 3.8 ms | 618 MiB | 8/8 |
| `sentence-transformers/all-MiniLM-L6-v2` | 384 | 256 | 47 ms | 2.1 ms | 574 MiB | 8/8 |
| `BAAI/bge-base-en-v1.5` | 768 | 512 | 47 ms | 10.3 ms | 963 MiB | 8/8 |

- **`bge-small` is pinned** (revision `5c38ec7c405ec4b44b94cc5a9bb96e735b38267a`, cosine distance on normalized vectors). The fixture does not separate the three; the input limit and cost do. MiniLM's 256-token limit would silently truncate chunks in the 300–500-token range `rag-and-qa.md` asks for, and `bge-base` is 2.7× slower, 345 MiB larger and doubles vector size for no measured gain.
- The peak RSS includes the `torch` runtime, which `mlx-whisper` already pulls in, so `sentence-transformers` adds little to the install. In the server, running the indexer raised peak process RSS from 1.93 GB to 2.33 GB (5 synthetic phones).
- STT latency with and without the indexer running was the same within noise (`logs/rag.md`).

## Reasoning model selection (measured, CON-09)

Measured on the reference laptop with `scripts/measure_reasoning.py` (each candidate in its own process, cached,
a Q&A-sized prompt of about 1,200 tokens) and `scripts/calibrate_qa.py --honesty --repeats 3` (12 answerable and 12
unanswerable questions over a fixture meeting, `logs/qa.md`).

| Candidate | Load (cached, incl. warm-up) | Peak MLX | Prompt | Generation | Context | Grounded correct | Fabricated | Answer time median / max |
|---|---|---|---|---|---|---|---|---|
| `Meta-Llama-3.1-8B-Instruct-4bit` (**pinned**) | 1.7 s | 5.49 GB | 352 tok/s | 49 tok/s | 128k | 36/36 | 0/36 | 3.9 / 4.8 s |
| `Qwen2.5-7B-Instruct-4bit` | 0.7 s | 5.00 GB | 370 tok/s | 53 tok/s | 32k | 33/36 | 0/36 | 5.2 / 6.8 s |

- **Llama 3.1 8B is pinned**: both never answered an unanswerable question, but Qwen consistently declined one
  answerable question (the invited-user count) and its prompts tokenize longer, so its answers were slower. The
  cost is 0.5 GB more memory. The Llama weights are under the Llama 3.1 Community License.
- An answer takes about 4 s, most of it prompt processing (five excerpts); generation is 50 tokens/s.
- **Running with STT.** Both run on the GPU through MLX in separate threads without errors. With a question every
  8 s while synthetic phones talk continuously, STT post-window latency rose from 1.69 / 1.69 s (median / p95) to
  1.72 / 2.23 s with one phone, 1.97 / 2.88 s to 2.21 / 3.28 s with two, and 3.76 / 5.26 s to 4.46 / 6.70 s with
  five; no window was dropped. MLX work cannot be preempted, so the priority gate only delays the start of a
  generation (`stt-pipeline.md`); this is the accepted, measured cost.
- The runtime (`mlx-lm` 0.31) upgraded `transformers` to 5.x in the environment; `sentence-transformers` 3.4 and
  the embedding model tests still pass on it.

## Scheduling priority across resource types

STT has scheduling priority over `reasoning` calls (see `stt-pipeline.md`) — a live transcript stalling is a worse demo failure than a summarization or Q&A answer taking an extra second or two. This is a scheduling policy, not a resource-type configuration, and lives in the STT pipeline's worker pool logic. It is implemented as a cooperative gate (`server/pipeline/priority.py`): reasoning and embedding callers `await` it before starting and wait while the STT backlog is high, up to a maximum wait (`priority_max_wait_s`). MLX inference cannot be preempted, so a job already running is not interrupted; how much this interferes is measured in CON-15.

## Cloud fallback activation

Cloud resource-type backends exist behind the exact same adapter interface as their local counterparts (`transcribe_window(...)`, `generate(...)`) and are selected purely by configuration — never invoked automatically from within the live transcription or Q&A critical path (ADR-06). Activating one is a deliberate action (a config flag or an explicit "use cloud" toggle in the dashboard), logged as such, not a silent runtime failover — a silent failover risks masking a real local-performance problem that should instead be fixed or tuned before demo day.

## Model swap procedure

Because every reference to a model is indirect (ADR-14): change the resource-type mapping in this document (and the corresponding config value in code), restart the relevant adapter, done. No application code in `stt-pipeline.md`, `rag-and-qa.md`, or `summarization.md` should ever need to change for a model swap.
