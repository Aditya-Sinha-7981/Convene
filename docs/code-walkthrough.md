# Convene: code walkthrough and design rationale

This document explains what we built, how the code works, and why each choice was made. Every claim points to
the file (and usually the line) where it lives, so you can open the code while answering a question. Where
something was only tested synthetically, or not tested at all, the document says so. Examiners tend to probe
exactly those gaps, and an honest "we measured X, we did not measure Y" is a stronger answer than a vague "it works".

Sources: `AGENTS.md`, the work orders in `convene-tasks/` (CON-01 to CON-18), the work logs in `logs/`, the decision
records in `docs/decisions.md` (ADR-01 to ADR-34), and the code in `server/`, `client/`, `scripts/` and `tests/`.

---

## Contents

1. [The product in one minute](#1-the-product-in-one-minute)
2. [The core idea: the phone is the speaker](#2-the-core-idea-the-phone-is-the-speaker)
3. [Architecture at a glance](#3-architecture-at-a-glance)
4. [Life of one sentence (end-to-end trace)](#4-life-of-one-sentence-end-to-end-trace)
5. [Technology choices and why](#5-technology-choices-and-why)
6. [How we worked](#6-how-we-worked)
7. [Component deep dives](#7-component-deep-dives)
   - 7.1 Transport: WebRTC and signaling
   - 7.2 HTTPS and the network
   - 7.3 Storage and the audit stream
   - 7.4 Speech-to-text pipeline
   - 7.5 Attribution and correction
   - 7.6 Live dashboard
   - 7.7 RAG ingestion: chunking and embeddings
   - 7.8 Live Q&A
   - 7.9 Summary and action items
   - 7.10 DOCX export
   - 7.11 History, cross-meeting Q&A, rename and delete
   - 7.12 Action-item lifecycle
   - 7.13 Policy repository
   - 7.14 Periodic reports and multi-meeting summaries
   - 7.15 Emailing the minutes
   - 7.16 Frontend
8. [Cross-cutting principles in the code](#8-cross-cutting-principles-in-the-code)
9. [Testing and verification](#9-testing-and-verification)
10. [Timeline](#10-timeline)
11. [What is not done, and known limitations](#11-what-is-not-done-and-known-limitations)
12. [Likely questions with short answers](#12-likely-questions-with-short-answers)
13. [File map cheat sheet](#13-file-map-cheat-sheet)

---

## 1. The product in one minute

Convene turns the phones already in a meeting room into separate microphones. Each person opens a link (a QR code)
on their phone, types their name and allows microphone access. Their phone streams audio over the local Wi-Fi to one
laptop. The laptop:

1. transcribes each phone's audio locally (Whisper on Apple Silicon),
2. labels every line with the person who joined from that phone,
3. shows a live transcript on a dashboard where a wrong label can be fixed in two clicks,
4. answers questions about the meeting while it is still running, with citations to the exact lines,
5. writes structured minutes (summary and action items) when the meeting ends and exports them as a DOCX,
6. keeps a searchable history of past meetings, tracked action items, a policy-document library, periodic
   reports, and can email the minutes to participants.

The target was a reliable hackathon demo on one laptop (MacBook Pro M4 Pro, 24 GB) with 1 to 10 phones, working
without internet by default.

---

## 2. The core idea: the phone is the speaker

**Every other meeting-transcription tool records one mixed audio stream and then guesses who spoke (acoustic
diarization).** Diarization is hard and error-prone, and a confidently wrong speaker name destroys trust in the
minutes.

Convene avoids the problem: each person speaks into their own phone, so **which WebRTC connection the audio arrived on
tells us who spoke**. We do not need to guess. That is ADR-02 (`docs/decisions.md`), and it is the project's main
technical differentiator.

In code, the attribution decision for a normal (non-shared) phone is literally a lookup, not a model:

```python
# server/attribution/service.py:83-90  (AttributionService._insert)
if device.is_shared:
    participant_id, method, confidence = None, "generic_unresolved", self.config.unresolved_confidence
else:
    if len(people) != 1:
        raise ValidationError("a non-shared device must have exactly one participant")
    participant_id, method, confidence = people[0].participant_id, "device", self.config.device_confidence
```

Two consequences we designed for:

- **Cross-device bleed.** A phone near someone else can pick up their voice. Diarization does not solve this; we
  handle it with a per-device voice-activity gate (ADR-05) and a cross-device bleed filter (ADR-32). See 7.4.
- **Honest labels.** Every line stores an attribution method and a confidence, and every line can be corrected with
  the original preserved (ADR-04, ADR-17). A shared phone gets a generic label ("Speaker on Phone 2") marked low
  confidence, never a guessed name.

---

## 3. Architecture at a glance

One Python process on the laptop (ADR-01). FastAPI serves REST, WebSockets and static pages. aiortc terminates the
phones' WebRTC audio. SQLite (one file, `data/convene.db`) with the `sqlite-vec` extension stores everything,
including vectors.

```
 Phones (browser, join page client/app.js)
   │  WebRTC audio (Opus over UDP, LAN only)       WebSocket signaling /ws/signal/{meeting}
   ▼                                               ▼
 ┌──────────────────────────── server/ (one process) ─────────────────────────────┐
 │ transport/peers.py ── PeerManager: one DeviceSession per phone                 │
 │   │ per-device bounded queue + pump task                                       │
 │   ▼                                                                            │
 │ pipeline/  SttPipeline: EnergyVad → Segmenter → bleed.judge → SttScheduler     │
 │   │        (round-robin fair queue, drop-oldest) → adapter (MLX Whisper/Gemini)│
 │   ▼ TranscribedWindow callback (runtime.on_transcribed_window)                 │
 │ attribution/service.py  device → participant, confidence → Utterance row       │
 │   │ post-write hook                     │ audit.emit("utterance_created")      │
 │   ▼                                     ▼                                      │
 │ rag/indexer.py  chunk + embed      db.py subscribers → dashboard_hub.py        │
 │   (sqlite-vec)                        → /ws/dashboard/{meeting} (live UI)      │
 │                                                                                │
 │ rag/qa.py         explicit question → embed → KNN → threshold → Llama → cite   │
 │ summary/service.py meeting end → drain → prompt → JSON → validate → store      │
 │ export/            python-docx fixed template → data/exports/<id>.docx         │
 │ action_items/, policies/, reports/, mail/   post-meeting features              │
 └────────────────────────────────────────────────────────────────────────────────┘
                     SQLite + sqlite-vec: data/convene.db (WAL)
```

The wiring of all these services happens in one place, `Runtime.start` (`server/runtime.py:135`). It is the best
single function to show an examiner, because it lists every component and the hooks that connect them:

- `self.on_transcribed_window(self.attribution.accept)`: STT results go to attribution.
- `self.attribution.register_post_write_hook(self.indexer.enqueue)`: new lines get indexed for Q&A.
- `self.attribution.register_correction_hook(self.indexer.corrected)`: a correction re-embeds the chunk.
- `self.on_meeting_ended(self.indexer.meeting_ended, ...)` and `self.on_meeting_ended(self.summary.meeting_ended, ...)`:
  ending a meeting closes the index and starts the summary.
- `self.summary.on_ready = self.export.on_summary_ready`: a ready summary automatically renders the DOCX.
- `self.db.subscribe(self.hub.on_audit)`: every committed audit event can be pushed to dashboards.

---

## 4. Life of one sentence (end-to-end trace)

Priya says "Let's move the beta launch to the 14th" into her phone. This is what happens, in order.

| # | Step | Where |
|---|---|---|
| 1 | Her phone already registered over REST (`POST /api/meetings/{id}/devices`), which created a `Device` and a `Participant` row and a `device_registered` audit event. | `server/routes.py:163`, `server/registry.py:172` |
| 2 | The phone captures the mic with echo cancellation and noise suppression on, automatic gain control off. | `client/app.js:13` |
| 3 | Audio flows over a WebRTC peer connection. aiortc fires a `track` event; a receive loop converts each frame to 16-bit mono PCM. | `server/transport/peers.py` (`_watch`), `server/transport/audio.py:15` |
| 4 | Frames go into that phone's own bounded queue (250 frames, about 5 s). A per-device pump task pushes them to the sink. A slow sink fills only that phone's queue. | `server/transport/peers.py` |
| 5 | The STT pipeline is the sink. It resamples to 16 kHz and feeds the phone's `Segmenter`. | `server/pipeline/pipeline.py:98` |
| 6 | The `EnergyVad` marks each 20 ms frame voiced or not, against a rolling noise floor. | `server/pipeline/vad.py:49` |
| 7 | The segmenter opens a segment on the first voiced frame (with 200 ms pre-roll) and closes it after 600 ms of silence. | `server/pipeline/segmenting.py:77`, `_on_frame` |
| 8 | The bleed filter checks whether another phone heard the same speech at least 6 dB louder. If so, the segment is dropped and audited. | `server/pipeline/bleed.py:75` |
| 9 | The scheduler queues the segment in Priya's queue; workers take segments round-robin across phones. | `server/pipeline/scheduler.py:131` (`submit`), `_take` |
| 10 | The MLX Whisper adapter transcribes it in a thread (about 0.5 s). The result is checked against a hallucination blocklist. | `server/pipeline/mlx_whisper_adapter.py:101`, `scheduler.py` (`_process`) |
| 11 | A `TranscribedWindow` outcome is delivered to callbacks. Attribution inserts an `Utterance` (participant = Priya, method `device`, confidence 0.95) and emits `utterance_created` in the **same transaction**. | `server/attribution/service.py:68` |
| 12 | After commit, the database notifies subscribers. The dashboard hub turns the audit event into an `utterance` push with the server-computed `speaker_label` and `low_confidence`. | `server/db.py` (`_run_sync`), `server/dashboard_hub.py:79` |
| 13 | The browser reducer inserts the line in time order and redraws. | `client/dashboard_state.js` |
| 14 | The post-write hook enqueues the line for indexing. About 2 s later the meeting's open chunk is re-chunked and re-embedded (BGE-small), and it becomes searchable. | `server/rag/indexer.py:108`, `_index` at `:280` |
| 15 | Someone asks "What did we decide about the beta launch?" `QAService.ask` embeds the question, searches only this meeting's vectors, keeps chunks above similarity 0.50, and asks Llama 3.1 8B for an answer, citing the chunks it gave the model. | `server/rag/qa.py:149`, `:355` |
| 16 | At "End meeting", the STT queue is drained, the transcript is assembled, and Llama writes JSON minutes. They are validated, stored, and a DOCX is rendered. | `server/summary/service.py:110`, `server/export/service.py:36` |

---

## 5. Technology choices and why

| Layer | Choice | Why | Alternative rejected |
|---|---|---|---|
| Server | Python, one process, FastAPI + uvicorn | Every AI piece (Whisper, embeddings, LLM) is Python-native; one process means no IPC boundary to debug on stage (ADR-01) | Node transport + Python AI sidecar |
| Audio transport | WebRTC via `aiortc` | Built-in jitter buffering, packet-loss concealment, low latency; proven in our earlier DT-17 prototype (ADR-09) | WebSocket + `MediaRecorder` chunks (TCP head-of-line blocking, imprecise chunk timing) |
| Signaling | WebSocket, JSON, snake_case | Only needs join/offer/answer/leave; one casing across the whole API (ADR-16) | — |
| NAT traversal | None: no STUN/TURN, `iceServers=[]` | Everyone is on one LAN; a STUN server is an internet dependency (ADR-10) | Public STUN/TURN |
| Storage | SQLite, WAL mode, one file | Zero ops, one backup unit, fits one laptop (ADR-07) | Postgres |
| Vectors | `sqlite-vec` in the same file | One store to reason about; partition key per meeting (ADR-08) | Chroma (a second moving part) |
| STT | `mlx-whisper`, `whisper-large-v3-turbo`, pinned revision | Measured on the reference laptop: about 0.5 s per call, WER 0.000 on whole clips; distil was not faster (logs/stt.md) | distil-large-v3, whisper-small/base |
| Embeddings | `BAAI/bge-small-en-v1.5`, 384-d, CPU, sentence-transformers | 3.8 ms per chunk, 512-token input fits our 400-token chunks | MiniLM (256-token limit truncates chunks), bge-base (2.7× slower, no gain) |
| Reasoning LLM | `Meta-Llama-3.1-8B-Instruct-4bit` via `mlx-lm` | 36/36 grounded answers, 0/36 fabricated in our honesty test; Qwen2.5-7B missed 3 | Qwen2.5-7B-Instruct |
| Documents | `python-docx`, fixed template | The model produces data, code produces the file, so the file can never be malformed (ADR-12) | Model-written Markdown/DOCX |
| Frontend | Plain HTML/CSS/ES modules, no framework, no CDN | Works offline; nothing to download at the venue | React + CDN |
| Optional cloud | Gemini STT (ADR-31), Resend email (ADR-33), stdlib `urllib` only | Only by deliberate operator configuration, never an automatic fallback (ADR-06) | SDKs (extra dependencies for one POST) |

**Models are never named in application code** (ADR-14). They live in `config/convene.toml` under
`[models.stt]`, `[models.embedding]` and `[models.reasoning]`, each with an exact Hugging Face revision. Code asks for a
resource type (`build_adapter`, `build_embedding_adapter`, `build_reasoning_adapter`). Swapping a model is a config edit.

**Models load from the local cache only.** `scripts/provision_models.py` downloads them once while online. At
startup, a model missing from the cache makes the server refuse to start with a clear message rather than download
anything (`server/pipeline/mlx_whisper_adapter.py:41` `resolve_snapshot`, `server/rag/reasoning.py:90`).

---

## 6. How we worked

This is worth explaining, because it shows engineering discipline beyond the code.

1. **Architecture first, in `docs/`.** Requirements, architecture, data model, API, transport, STT, attribution,
   RAG, summarization and export were written before implementation. `docs/decisions.md` records 34 decisions,
   each with rationale, alternatives and trade-offs.
2. **Work orders.** `convene-tasks/` splits the build into tasks CON-01 to CON-18, each with scope, allowed files,
   contracts, tests, acceptance criteria, risks and open questions. The build order is in
   `convene-tasks/ALL-TASKS.md`:
   `01 baseline → 02 contracts → 03 storage → 04 transport → 05 STT → 06 attribution → {07 dashboard, 08 chunking → 09 Q&A, 10 summary → 11 DOCX} → 12 end-to-end`,
   then 14 history, 16 action items, 17 policies, 18 reports. (13 shared-device and 15 load validation are not done.)
3. **Contracts before code (CON-02).** The API, signaling messages, events and error codes were fixed in
   `docs/api.md` and `docs/transport.md` before the server was built, and `tests/test_api_contract_docs.py` checks
   the documents themselves (every JSON example parses, field names match the data model, error codes are in the
   catalog).
4. **Measure, then decide.** Model choices, window size, VAD margins, the Q&A threshold and the summary length limit
   were all chosen from measurements on the reference laptop, recorded in `logs/`, with the scripts that produced
   them in `scripts/measure_*.py` and `scripts/calibrate_qa.py`.
5. **Work logs.** Each workstream has a log in `logs/` recording what was built, commands, results, decisions and
   what was *not* tested. Every check is reported as Passed, Failed or Not run (with the reason).
6. **Honest verification.** A synthetic phone over loopback proves the server code; it does not prove a real phone
   works. The logs keep that distinction everywhere.

---

## 7. Component deep dives

Each section has: what it does, where the code is, how it works, why it was designed this way, and what was
measured.

### 7.1 Transport: WebRTC and signaling (CON-01, CON-04)

**Files:** `server/transport/signaling.py`, `server/transport/peers.py`, `server/transport/sessions.py`,
`server/transport/audio.py`, `client/app.js`.

**Flow for a phone:**

1. `POST /api/meetings/{id}/devices` registers the phone (REST). The phone generates its own UUID `device_id` and
   keeps it in `localStorage` under a meeting-scoped key (`client/app.js:15-21`), so closing and reopening the tab
   resumes the same identity.
2. The phone opens `/ws/signal/{meeting_id}` and sends `{"type":"join","device_id":...}`.
   `SignalingConnection._join` (`server/transport/signaling.py`) checks the meeting exists and has not ended, and that
   the device is registered, then replies `joined` with `is_reconnect` and `reconnect_count`.
3. The phone creates an `RTCPeerConnection`, **waits for ICE gathering to complete** (non-trickle ICE, capped by a
   timeout, `client/app.js:156`) and sends one `offer`.
4. `PeerManager.handle_offer` (`server/transport/peers.py:80`) validates the SDP (a string, at most the size limit,
   parses, has an audio section), creates a fresh `RTCPeerConnection(RTCConfiguration(iceServers=[]))`, and returns
   the answer.
5. On `connectionstatechange == "connected"` the server records `device_connected` or `device_reconnected`
   (`server/registry.py:268`). On `failed`/`disconnected`/`closed` it records a disconnect.

**Design decisions and why:**

- **REST registers, WebSocket only attaches** (ADR-16). One place creates identity; the dashboard can show a phone
  before its audio connects.
- **One `join` message for first connect and reconnect.** The server decides whether it is a reconnect (it checks
  for earlier `connected` events), so client and server cannot disagree.
- **Non-trickle ICE.** With no STUN servers there are only host candidates, so waiting for gathering is fast and
  avoids candidate-ordering bugs.
- **Socket and peer have separate lifetimes** (ADR-16). If only the signaling WebSocket drops, audio can keep flowing
  and the device stays `connected` (`PeerManager.socket_closed`, `peers.py:128`).
- **ICE restart is refused.** We found during CON-04 that aiortc 1.15 accepts a re-offer but keeps the old ICE
  connection, and the new client connection fails. So an `ice_restart` offer gets a non-fatal
  `renegotiation_failed` and the phone builds a fresh peer instead (`peers.py:84-90`). This is a real finding from
  testing, not a guess.
- **Reconnect is counted when the new peer connects**, not at `join`, so a phone that joins but never gets audio
  through is not counted as reconnected.
- **Client retry:** exponential backoff from 1 s to 10 s, retry on the browser's `online` and `visibilitychange`
  events, and a manual Retry button after repeated failures (`client/app.js:97-113, 266-268`).
- **Device isolation.** Each phone has its own `DeviceSession`, its own lock, its own queue and pump task. A
  malformed message resets only that phone (`reset_after_error`, `peers.py:139`). A test hangs one phone's sink
  forever while another keeps streaming (`tests/test_transport_integration.py`).
- **Bearer identity accepted as a limitation** (ADR-15). Anyone on the LAN who knows a `device_id` could take it
  over. A per-device secret would break reconnection after a restart or need a schema change; the threat (a
  deliberate hijacker in the same room) is outside the model, and a takeover shows up as a `device_reconnected`
  event.

**Pause vs leave (ADR-32).** "Pause microphone" disables the audio track but keeps the connection and seat
(`client/app.js:61-63`: `track.enabled = !value`). "Leave meeting" sends `leave`, which closes the peer and records
`device_left`.

**Verified:** 302 tests at CON-04 over loopback with a synthetic aiortc phone (`tests/support/synthetic_phone.py`);
20 deliberate breakages each caught. One real phone joined over the trusted hostname on 2026-09-25 and its audio
reached STT and the dashboard. **Not run on real phones:** Wi-Fi drop recovery, multi-phone isolation, screen lock.

### 7.2 HTTPS and the network (CON-04, CON-04B)

**The problem:** mobile browsers only allow `getUserMedia` (microphone) on a secure origin, so the join page must be
HTTPS with a certificate the phone trusts.

**Two modes, both in `server/network.py`:**

1. **Development:** the laptop's LAN IP with a `mkcert` certificate. Works, but every phone would need our local CA
   installed: unusable for a QR-code join.
2. **Demo (ADR-20):** a real public hostname with a publicly trusted certificate (ACME DNS-01). Before a session, the
   operator runs `scripts/update_dns.py`, which points a DNS-only Cloudflare A record at the laptop's **private**
   hotspot IP. The phone resolves the name, gets a private IP, and then everything (page, WebSocket, WebRTC) stays on
   the local network.

**Why the DNS update is a separate script:** the server never holds DNS credentials or calls Cloudflare. A remote
DNS change is a visible, deliberate operator action. `scripts/start_demo.sh` deliberately does not load `.env`
(where the Cloudflare token lives).

**Startup checks** (`check_certificate`, `network.py:185`): the certificate must cover the advertised address or
hostname (including one-label wildcards) and not be expired; otherwise the server exits with the exact `mkcert`
command to run. Near-expiry and stale DNS are warnings.

**Honest caveat:** this mode needs weak internet for the DNS lookup, so it is not a fully offline claim. Only the
initial name resolution uses the internet.

**Other network details:** the QR code is an inline SVG returned by `POST /api/meetings` (`network.py:103`).
FastAPI's `/docs` pages are disabled because they load assets from a CDN.

### 7.3 Storage and the audit stream (CON-03)

**Files:** `server/db.py`, `server/audit.py`, `server/audit_catalog.py`, `server/registry.py`,
`server/repositories/*`, `server/migrations/0001` to `0011`.

**Migrations.** Plain numbered SQL files; the schema version is SQLite's `PRAGMA user_version`
(`server/db.py:61` `migrate`). Numbering must stay contiguous. The connection loads `sqlite-vec` before migrations,
because a database containing a `vec0` table cannot even be read without the extension (found in the CON-03 spike).

**Concurrency model** (`server/db.py:135` `Database`):

- One connection, WAL journal, a busy timeout, foreign keys on.
- All writes go through `await db.run(fn)`, which runs `fn(tx)` on a **single dedicated worker thread**
  (`ThreadPoolExecutor(max_workers=1)`), so a slow write never blocks the asyncio event loop that is receiving
  audio.
- Every transaction starts with `BEGIN IMMEDIATE`, taking the write lock up front, so two writers cannot interleave.
- Each `run` is one short transaction; a failing write for one phone rolls back only itself.

*Why:* the event loop handles audio for every phone. If a database write blocked it, every phone's audio would
stall. Measured: ten 50 ms writes serialize (at least 0.45 s total) while a 5 ms heartbeat coroutine keeps
ticking.

**The single audit path** (ADR-13). `emit()` (`server/audit.py:18`) is the only way to write an `AuditEvent`:

- It validates the event type against a catalog (`server/audit_catalog.py`) and requires the payload keys to be
  **exactly** the catalogued keys. This makes it structurally impossible to put transcript text in an audit row.
  A test asserts the code catalog equals the table in `docs/data-model.md`.
- It assigns `seq = MAX(seq) + 1` inside the caller's transaction. Because of `BEGIN IMMEDIATE`, `seq` is unique and
  strictly increasing (150 concurrent emits got 1 to 150 in order in a test).
- It runs **inside the caller's transaction**, so a state change and its audit record commit or roll back together.
  For example, `record_connection_event` writes the `ConnectionEvent` projection and its `AuditEvent` atomically,
  and they share the same `event_id` (`server/audit.py:65`).
- Subscribers (the dashboard hub) are notified only **after commit**, in commit order
  (`loop.call_soon_threadsafe(self._deliver, ...)` in `server/db.py`). A dashboard can never show something that was
  rolled back.

**Why `seq` matters:** it is the cursor for dashboard resynchronization (7.6) and for staleness: "the summary is
stale if an `utterance_created` or `utterance_corrected` exists with a higher `seq` than the summary's input"
(`server/attribution/staleness.py:8`). No stored `stale` flag that could drift (ADR-18).

**Registry** (`server/registry.py`): `create_meeting`, `register_device` (idempotent: a replay returns the stored rows
and writes nothing, `:172`), `record_device_connected/disconnected/left`, `end_meeting`, `erase_meeting`, and
`reconcile_after_restart` (`:323`). After a crash, devices that the database still thinks are `connected` are marked
`disconnected` with reason `server_restart`. Verified by SIGKILLing a process and restarting: the phone came back as
a reconnect of the same participant.

**Durability trade-off:** `synchronous = NORMAL` with WAL survives an application crash but could lose the last
transaction on power loss. Acceptable for a demo laptop; documented.

**Data model essentials** (`docs/data-model.md`): `Meeting` (created → live → ended), `Device`, `Participant`,
`Utterance` (text, `t_start`/`t_end`, `stt_confidence`, `attribution_method`, `attribution_confidence`, `corrected`,
`original_participant_id`), `ConnectionEvent`, `AuditEvent`, `ModelExecution` (one row per model call),
`TranscriptChunk` + `TranscriptChunkVector`, `QAQuery`, `Summary`, `ActionItem`, `ActionItemNote`, `Export`,
`PolicyDocument`/`PolicyVersion`/`PolicyChunk`, `ParticipantEmail`. All IDs are UUID v4; all timestamps are UTC ISO
8601 with milliseconds and a `Z`, so string order equals time order.

**Raw audio is never stored.** Only the transcript and metadata are saved.

### 7.4 Speech-to-text pipeline (CON-05 and later)

**Files:** `server/pipeline/vad.py`, `segmenting.py`, `bleed.py`, `scheduler.py`, `priority.py`, `pipeline.py`,
`adapter.py`, `mlx_whisper_adapter.py`, `gemini_adapter.py`, `romanize.py`, `windowing.py` (fixed-window fallback).

#### 7.4.1 Voice-activity detection (ADR-05)

`EnergyVad` (`server/pipeline/vad.py:28`) is a dependency-free energy gate over 20 ms frames:

- For each frame it computes the level in dBFS.
- The **noise floor is the 10th percentile of the last 5 s of frame levels** for that phone.
- A frame is voiced if its level is above `max(-50 dBFS, noise_floor + 9 dB)`, with a 5-frame hangover.

*Why an adaptive percentile:* the first version only learned the noise floor from frames it had rejected, so steady
noise louder than the threshold opened the gate forever (measured: 26 of 26 noise windows passed). The rolling
percentile fixed it: silence and steady noise are gated, speech passes. A 12 dB margin gated more noise but lost
speech at low signal-to-noise (exactly the bleed situation), so 9 dB was chosen.

*Why a VAD at all:* (1) Whisper on silence or noise returned "Thank you." with confidence 0.60 to 0.79. Whisper's own
`no_speech_prob` read 0.00, so the model cannot tell us it heard nothing; the gate must. (2) It saves compute. (3) It
is the first defense against bleed: a phone hearing a distant voice is less likely to clear its own gate.

#### 7.4.2 Speech segments instead of 1-second windows (ADR-19)

The original design sent fixed ~1 s windows to STT. Measurements killed it:

| Finding (reference laptop, turbo) | Number |
|---|---|
| One model call costs about the same whether the audio is 1 s or 8 s | ~0.5 s |
| So one worker sustains | ~2 calls/s |
| 1 s windows, 5 phones all talking | 55% of windows dropped, ~4 s lag |
| 1 s windows cut words in half | WER 0.094 vs 0.000 on whole clips ("very tough. height." for "very tight.") |
| More workers | 1 → 4 workers: 1.94 → 2.06 calls/s (GPU-bound, no gain) |

So each phone's audio is cut **by its own VAD into one segment per stretch of speech** (`Segmenter`,
`server/pipeline/segmenting.py:49`):

- Starts at the first voiced frame with 200 ms pre-roll (so the first syllable is not clipped).
- Ends after 600 ms of silence, keeping a 200 ms tail.
- Capped at 8 s; a long monologue is cut at the **quietest frame** in the last 1.5 s (`_cut_at_the_quietest_point`),
  to avoid cutting mid-word.
- A voiced burst under 300 ms (cough, click) is discarded.
- A gap in the stream (reconnect) flushes the open segment so no segment straddles a gap.

Result with segments: five phones continuously talking, **zero drops**, median 1.95 s from end of speech to text.
One line per sentence is also the right unit for transcript rows, Q&A citations and minutes. Fixed windows remain a
one-line config fallback (`segmentation = "fixed"`).

**Time base:** timestamps come from the server's wall clock at frame receipt (UTC), through a `StreamClock` that
re-anchors after gaps. They include network delay, so they are not sample-accurate. That is fine for ordering and
display.

#### 7.4.3 Cross-device bleed filter (ADR-32)

`judge()` in `server/pipeline/bleed.py:75`. Every phone keeps a 30 s history of 20 ms frame levels. When a segment
passes its own VAD, we compare its loudness envelope with the other phones in the same meeting over the same time
span, trying lags up to ±300 ms. The segment is **dropped only if both**:

- the envelopes correlate (Pearson ≥ 0.7), meaning it is the same sound, **and**
- the other phone heard it at least 6 dB louder, meaning the speaker was closer to the other phone.

*Why both conditions:* level alone would drop a quiet person talking over a loud one; correlation distinguishes "the
same speech heard twice" from "two people talking at once". Every doubtful case (missing history, equal levels,
errors) **keeps** the segment, because a missing line is less harmful than silently losing real speech. Drops are
audited as `stt_window_dropped` with reason `bleed`. It runs before STT, so it also saves model calls.

For the level comparison to mean anything, the phone turns automatic gain control **off** (`client/app.js:13`),
otherwise each phone would normalize its own volume.

**Verified:** synthetic tests only (a copy 12 dB quieter and 40 ms delayed is dropped; two simultaneous speakers are
kept). **Not run on real phones.**

#### 7.4.4 Scheduler: fairness and overload (`server/pipeline/scheduler.py:72`)

- **Per-device queues, round-robin** (`_take`). A talkative phone cannot starve a quiet one.
- **Bounded:** at most `queue_max = 4` segments per phone. When full, the **oldest** is dropped
  (`submit`, `:131`), counted, and audited. *Why drop-oldest:* in a live meeting a stale line is worth less than the
  current one, and the queue must not grow without bound.
- **Never blocks the audio path:** `submit` just appends and releases a semaphore.
- **Model calls in a thread** with a 20 s timeout (`run_in_executor` + `wait_for`), so the event loop stays free.
  Measured event-loop lag with 5 phones: ≤ 8 ms.
- **Failure isolation:** an adapter error marks that one segment `failed`, audits `model_error`, and the next segment
  proceeds. A bug in the scheduler itself is caught by the worker, counted as a worker restart, and the segment is
  reported failed instead of vanishing.
- **Hallucination blocklist** (`_is_hallucination`): "thank you", "thanks for watching", etc. are suppressed **only when
  the segment was weak** (low voiced fraction, or voiced frames less than 10 dB over the noise floor). The same words
  in a strong segment are kept.
- **One `ModelExecution` row per call**, including failures, so every model invocation is on record.
- **`drain(meeting_id, timeout)`**: used at meeting end so the summary includes the last words.

**Compute priority** (`server/pipeline/priority.py:22`): before a Q&A answer, a summary, or an embedding batch, the
code calls `wait_for_turn()`, which waits while the STT backlog is above 4 segments, for at most 3 s. Live
transcription has priority over everything else. Measured: a summary during 5 continuously talking phones still
delayed STT noticeably (17.5 s median), which is why the demo summarizes at meeting end, when STT has been drained.

#### 7.4.5 Adapter contract and Whisper settings

`SttAdapter` (`server/pipeline/adapter.py:31`): `load()`, `transcribe_window(device_id, window_id, audio) → SttResult`,
`close()`. `FakeAdapter` lets all non-model tests run without weights.

`stt_confidence` = `exp(duration-weighted mean avg_logprob) × (1 − mean no_speech_prob)`, clamped to 0 to 1
(`summarize_segments`, `mlx_whisper_adapter.py:26`). It is documented as **uncalibrated**: it ranks how sure the model
was of its tokens, and it does not tell speech from silence (that is the VAD's job). It is stored separately from the
attribution confidence.

#### 7.4.6 Language: English and romanized Hindi (ADR-21 → ADR-24)

- First we set `language = "auto"` (ADR-21). On a real phone, Hindi speech came out as **Spanish** ("Gracias", "¿Qué
  agarró?").
- The fix (ADR-24): always decode in **English mode** with a short code-mixed prompt:
  `hindi_prompt = "Okay, so the meeting kal hai. Haan, main dekh lunga. Theek hai."` (`config/convene.toml`). Whisper then
  writes Hindi in Latin letters, the way people type Hinglish ("Tum loog kya soch rahe ho?"), and English is
  unchanged (7 of 7 English clips identical with and without the prompt).
- Rejected: decoding Hindi as `hi` (Devanagari output, which users did not want); running a language detector first
  (+480 ms per segment).
- Caveat: measured only on macOS text-to-speech voices; real Hindi speech was not measured.

#### 7.4.7 Demo-only Gemini connector (ADR-31)

Local STT on real phone audio was judged not good enough for the demo, so we added `GeminiAdapter`
(`server/pipeline/gemini_adapter.py`): the same `transcribe_window` contract, one REST `generateContent` call per
segment with a WAV inline, stdlib HTTP, one retry on 429/5xx, the API key only in a header and never in logs.

- Selected **only** by the operator's environment at startup (`CONVENE_STT=gemini`), via `apply_stt_environment`
  (`server/config.py:342`). The config file and default stay local. There is no automatic fallback in either
  direction (ADR-06).
- The server prints a warning: cloud mode needs internet and sends meeting audio to Google, so no offline or privacy
  claim holds for that run.
- Gemini sometimes returned Devanagari or English translations. The prompt was tightened, and
  `server/pipeline/romanize.py` deterministically converts any Devanagari to Hinglish Latin spelling.

*Why this was easy:* because of the adapter contract (ADR-14), adding a backend touched no VAD, scheduling,
attribution or storage code.

### 7.5 Attribution and correction (CON-06)

**Files:** `server/attribution/service.py`, `labels.py`, `views.py`, `staleness.py`.

**Attribution** (`AttributionService.attribute`, `service.py:68`): only `ok` outcomes become utterances. Per
section 2: a non-shared phone gives its single participant, method `device`, confidence 0.95. A shared phone gives
no participant, method `generic_unresolved`, confidence 0.2 (a safe stub: the enrollment classifier, CON-13, is not
built). The insert and its `utterance_created` audit event share one transaction.

`accept()` is the STT callback; it schedules the write and returns immediately, so attribution never blocks STT.

**Correction** (`AttributionService.correct`, `service.py:105`; route `POST …/utterances/{id}/correct`):

- The body has exactly one of `participant_id` (any participant in the meeting) or `display_name` (reuses an exact
  name match or creates a new participant on that phone, which is how you "name" an unresolved speaker).
- The row gets `attribution_method = manual_correction`, confidence 1.0, `corrected = 1`.
- **First original wins:** `original_participant_id` keeps what attribution originally said, even after several
  corrections. The full from/to state of every correction goes into the `utterance_corrected` audit payload
  (ADR-17), so the complete history is recoverable without extra columns.
- Correcting to the same person is allowed and means "confirm" (clears the low-confidence marker). An identical replay
  writes nothing.

**The threshold lives on the server** (ADR-17): `low_confidence_threshold = 0.8` in config. The server sends a
boolean `low_confidence` and a ready-made `speaker_label` (`labels.py`). The browser never compares numbers, so the
dashboard, summary and DOCX cannot disagree about what "low confidence" means.

**Hooks:** post-write and correction hooks (indexer) run as isolated tasks; a failing hook is audited as `hook_failed`
and never affects the transcript.

### 7.6 Live dashboard (CON-07)

**Files:** `server/dashboard_hub.py`, `client/dashboard.html/js/css`, `client/dashboard_state.js`.

**Server side** (`DashboardHub`, `dashboard_hub.py:60`):

- It subscribes to committed audit events (`on_audit`, which only enqueues because it runs on the event loop).
- `_translate` turns each audit event into dashboard messages (`utterance`, `utterance_updated`, `device_status`,
  `meeting_status`, `connection_event`, `qa_answer`, `summary_ready`, `export_ready`, ...), reading the current row
  from the database. Every durable message carries the audit `seq`.
- A separate 1 s loop pushes `device_gauges` (last-audio age, duration, STT backlog, drops). Gauges are live-only and
  never stored (ADR-18: durable state comes from the audit stream; gauges are ephemeral).
- Each dashboard socket has its own bounded send queue; a slow browser overflows only itself.
- `/ws/dashboard/{id}` is read-only: client frames are ignored.

**Resync without losing or duplicating lines** (ADR-18, `client/dashboard.js:254-261`):

1. Open the WebSocket and **buffer** incoming events.
2. Fetch the snapshot (`GET /api/meetings/{id}` and `…/transcript`), which carries `as_of_seq`.
3. Apply the snapshot, then replay buffered events in `seq` order, ignoring any at or below `as_of_seq`.

This works after a dashboard reconnect or a server restart because `seq` is durable.

**Client reducer** (`client/dashboard_state.js`): pure functions, no DOM, no network, no threshold. Lines are sorted
by `t_start` then `utterance_id`; each row keeps a version (`seq`) so an older event never overwrites a newer one; a
correction replaces the whole row. Tested with Node (`tests/js/dashboard_state.test.mjs`), including 1,500-line
ordering.

**UI features:** device health cards, chat-style transcript grouped by speaker run with coloured mascot avatars,
low-confidence bubbles, correction dialog, "Ask the room" Q&A panel, QR/join card, End meeting, stale-feed banner.

### 7.7 RAG ingestion: chunking and embeddings (CON-08)

**Files:** `server/rag/chunker.py`, `indexer.py`, `embedding.py`, `vector_store.py`; migrations 0003/0004.

**Chunker** (`chunk_utterances`, `chunker.py:81`), pure and deterministic:

- Each line is rendered as `[Speaker, HH:MM:SS] text` (elapsed time since the meeting started), so the speaker and
  time are part of the embedded text.
- Target 400 tokens, hard cap 448 (below BGE's 512 input limit).
- Once the target is reached, a chunk ends at the next **speaker change**; the hard cap always wins.
- Never cuts inside an utterance, except one single over-long utterance, which is split at sentence boundaries into
  chunks of its own. So chunk ranges never overlap.

**Indexer** (`TranscriptIndexer`, `indexer.py:50`):

- Every trigger (new line, correction, meeting end, restart recovery) runs one **reconciliation** per meeting under a
  lock (`_index`, `:280`): compare stored chunks with the current transcript and re-embed only chunks whose text, range
  or status changed.
- The meeting's last chunk is an **open tail**: re-chunked and embedded 2 s after new speech (the settle window), so
  it is searchable while still growing. It closes on target size at a speaker change, on the hard cap, or when the
  meeting ends.
- A correction changes the speaker name inside the chunk text, so the chunk is re-embedded in place (same `chunk_id`).
- Embedding failures back off (1 s doubling to 30 s) instead of retrying every few seconds forever.
- Embedding waits for its turn behind STT (compute priority).

*Why the rewrite:* a code review of the first version found three real bugs, all with reproducing tests: lines
written after the end-of-meeting flush were never indexed; a late line between two closed chunks was never indexed;
and concurrent rewrites hit a `UNIQUE` constraint. The reconciliation design fixed all three (13 of 16 new tests fail
on the old indexer).

**Vector store** (`VectorStore`, `vector_store.py:24`):

- `TranscriptChunkVector` is a `vec0` virtual table: `chunk_id` TEXT primary key, `meeting_id` **partition key**,
  `float[384]`, cosine distance.
- `search()` filters by meeting **inside** the KNN query (`WHERE embedding MATCH ? AND k = ? AND meeting_id = ?`), so
  one meeting's vectors can never leak into another's results.
- `guard_model()`: `TranscriptIndexMeta` stores the one embedding model and dimension. Startup refuses a different
  model rather than mixing incompatible vector spaces.

**Measured:** BGE-small 3.8 ms per chunk, 618 MiB; indexing did not slow STT (same latencies with and without); a new
line is searchable in about 2 s (dominated by the settle window).

### 7.8 Live Q&A (CON-09)

**Files:** `server/rag/qa.py`, `retrieval.py`, `reasoning.py`, `citations.py`; route
`POST /api/meetings/{id}/qa`.

**Pipeline** (`QAService.ask`, `qa.py:149` → `_answer` → `_search_and_generate`, `qa.py:355`):

1. Validate (non-empty, at most 500 characters).
2. Check the index state. Nothing transcribed yet → `no_grounding` (`nothing_transcribed_yet`). Index healthy but
   still catching up → `no_grounding` (`not_indexed_yet`). Index failed or more than 30 s behind → `failed`
   (`index_unavailable`).
3. Embed the question with the **same** BGE model.
4. KNN search **scoped to this meeting** (`search_meeting`, `retrieval.py:37`), over-fetching and then filtering:
   only `ready` chunks, and only chunks whose first line was written **at or before the question** (the as-of rule),
   so an answer cannot cite something said after it was asked.
5. **Threshold:** keep chunks with cosine similarity ≥ 0.50, top 5. **If none pass, the model is not called at
   all**: `no_grounding` (`no_relevant_evidence`).
6. Build the prompt (`build_messages`, `qa.py:89`): excerpts fenced as `<excerpt n>` data, the system prompt says
   the excerpts are untrusted data, not instructions (**prompt-injection defense**; a closing fence inside the text
   is neutralized), and to reply exactly `NO_GROUNDING` if the answer is not there.
7. Generate with Llama at temperature 0, 200 tokens max, 30 s deadline. One generation at a time (a lock), after
   STT's turn.
8. If the model says `NO_GROUNDING` → `no_grounding`. Otherwise `clean_answer` strips inline references like
   `(Priya, 00:12:03)` (ADR-26).
9. Persist a `QAQuery` row and a `qa_query` audit event (with the reason and best similarity), then return and push.

**Three outcomes, always HTTP 200** (ADR-15, ADR-22): `answered`, `no_grounding` ("the meeting didn't say"),
`failed` ("the system is broken"). Keeping these distinct is the honesty requirement: "I don't know" must never look
like an error, and an error must never look like "nothing relevant".

**Citations are the chunks given to the model** (ADR-22), resolved from stored rows (`citations.py`), never parsed
from the model's text. The model cannot invent a citation. Clicking one scrolls to and highlights the lines.

**Why the threshold is the main guard:** a local 8B model will sometimes answer from general knowledge despite
instructions. So the guard that does not depend on the model comes first: retrieval. The `NO_GROUNDING`
instruction is the second guard.

**Calibration** (`scripts/calibrate_qa.py`, fixture `tests/fixtures/qa/product_sync.json`): answerable questions had
best similarity 0.521 to 0.746; unanswerable 0.400 to 0.675. The classes **overlap**, so no threshold separates them
perfectly. 0.50 keeps every answerable question and stops 5 of 12 unanswerable ones (including "capital of France")
before the model; the rest must be declined by the model.

**Model choice:** Llama 3.1 8B 4-bit: 36/36 grounded answers correct, **0/36 fabricated**, median 3.9 s. Qwen2.5-7B:
33/36, 0 fabricated. Memory with STT + embeddings + Llama: 5.7 GB RSS, 7.1 GB MLX peak.

**Residency:** the reasoning model loads at startup and stays in memory (`Runtime._start_reasoning`), so the first
question on stage has no cold start.

### 7.9 Summary and action items (CON-10)

**Files:** `server/summary/service.py`, `prompt.py`, `schema.py`, `views.py`; migration 0006.

**Trigger:** ending a meeting runs the `summary.meeting_ended` hook (`service.py:110`), which returns immediately and
starts a background task. `POST …/summarize` starts one manually. Only one attempt per meeting at a time
(`409 summary_in_progress`). An empty transcript starts nothing (`409 transcript_empty` on manual).

**Steps** (`_run`):

1. **Drain** (end trigger only): flush every phone's open segment, wait for queued STT and the attribution writes
   behind it, bounded by 15 s (`_drain`). *Why:* the last sentence before "End" must be in the minutes. A manual
   summary on a live meeting does not flush, because that would cut a sentence being spoken.
2. **Assemble** the transcript (`prompt.py:63`) with the *current* labels (a corrected line carries the corrected
   name), as `[HH:MM:SS] Speaker: text`.
3. **Count tokens** with the model's tokenizer. Above 16,000 prompt tokens → fail with `transcript_too_long`. **No
   truncation** (it would silently drop the end of the meeting) and no map-then-merge (a second prompt and failure
   mode). 16,000 tokens is about 50 minutes of nonstop talk, and far more of a normal meeting.
4. **Generate** JSON `{"summary": ..., "action_items": [{"text", "owner"}]}` at temperature 0.
5. **Validate** (`schema.py`): unwrap one code fence or surrounding prose, require both keys, every item has `text`
   and `owner` (null allowed). **No repair.** If invalid, retry once with a stricter prompt that names the error; a
   model error is not retried.
6. **Map owners** by case-insensitive exact display name; an unknown, ambiguous or generic label becomes null.
   **Never invent an owner.**
7. Store `Summary` (status `ready`) and `ActionItem` rows plus `summary_generated`, or a `failed` row with an error
   code. A failed attempt never replaces an earlier ready one (ADR-18).
8. `on_ready` renders the DOCX.

**Prompt iteration** (`logs/summary-export.md`): the first prompt was structurally valid but copied transcript lines
in the first person, lost a chair's own task and duplicated tasks. Iterations: third person, own words, one paragraph
per topic, include unowned "someone needs to" tasks, name "meet again / follow up" as non-tasks, and say an empty
list is correct.

**Measured:** 6 fixtures × 3 runs = 18 runs, 0 parse failures, 0 retries, no invented owners. Time: 14 to 16 s for a
17-minute fixture; about 100 s at the 16k limit. A `pending` row left by a crash is failed at the next startup
(`reconcile`).

**Staleness:** if a line is corrected or added after the summary was generated, the post-meeting page says the
summary is stale and offers Regenerate. Derived from `seq`, not a stored flag.

### 7.10 DOCX export (CON-11)

**Files:** `server/export/docx_renderer.py`, `service.py`; migration 0008.

- `render()` (`docx_renderer.py:27`) is a **pure function** of stored data: title, date, participants, Summary,
  Action items (with owner or "Unassigned", and status), Transcript appendix (with low-confidence marking). The model
  never touches formatting (ADR-12).
- Core document timestamps are fixed to 2000-01-01 so equivalent renders have identical document content
  (determinism is semantic, not byte-level: ZIP timestamps differ).
- `ExportService.ensure()` (`service.py:36`) renders only when there is no export or it is stale. Stale means a newer
  summary, a newer transcript change, a rename, or an action-item edit than the export's `seq`.
- **Atomic write:** render to a temp file, `fsync`, `os.replace` to `data/exports/<meeting_id>.docx`. A crash cannot
  leave a half-written file.
- Every attempt is a retained `Export` row (`pending`/`ready`/`failed`), so a failed re-render cannot hide an earlier
  good file.
- `GET /api/meetings/{id}/export?format=docx` never serves a stale file silently.

### 7.11 History, cross-meeting Q&A, rename and delete (CON-14, ADR-27)

- `GET /api/meetings` (`routes.py:135`): title search (with `%`/`_` escaped), date range, newest first, with counts
  and summary/export badges. Page `/history`.
- **History Q&A** `POST /api/qa` with `{question, mode: "history", meeting_ids: [...] | null}` (`qa.py:163`). Null means
  all **ended** meetings; at most 50 explicit ids; a live meeting in the list is `409 meeting_not_ended`.
- `search_meetings` (`retrieval.py:44`) runs **one partition-filtered KNN per meeting** and then merges, so one large
  meeting cannot crowd out the requested others. Each excerpt gets a `Meeting: <title> (<date>)` header, and
  citations carry the meeting title.
- If a meeting was indexed with a different embedding model, the whole query fails and names that meeting, rather
  than comparing incompatible vectors (`foreign_model_meetings`).
- The response lists coverage per meeting (searched, partial, not indexed, empty), so nothing is silently skipped.
- **Rename:** `PATCH /api/meetings/{id}`, audit `meeting_renamed`, makes the DOCX stale (the title is in it).
- **Delete** (ADR-27): `registry.erase_meeting` (`registry.py:113`) removes everything the meeting owns in one
  transaction, including its vectors, model-execution rows, Q&A rows and **its audit events**, then deletes the DOCX
  files. One `meeting_deleted` event (no content, just the id and counts) is written **before** the deletes so `seq`
  never goes backwards. Refused while a phone is connected or a summary is running. The UI requires typing the exact
  title. *Why permanent:* the project lead chose that a "deleted" transcript must not remain on disk.

### 7.12 Action-item lifecycle (CON-16, ADR-28)

- Items gain status `open | done | cancelled` and a nullable `due_date` (migration 0009 rebuilds the table, copying
  rows in order; a CHECK rejects impossible dates via `date(x) = x`).
- Edits (`update_action_item`, `server/action_items/service.py:79`) update the row and emit `action_item_updated` with
  only the changed fields in `from`/`to`, in one transaction. A no-op writes nothing.
- A later meeting can add an append-only `ActionItemNote` (optionally changing status). Note text never enters audit
  payloads.
- "Last changed" is **derived** from the audit stream, not stored (ADR-18 again).
- Lists show only each meeting's **current** summary's items; regenerating a summary starts fresh items and the UI
  warns first.
- Routes: `GET /api/action-items` (filters, sort, pagination), `GET/PATCH /api/action-items/{id}`,
  `POST …/notes`; page `/action-items`.
- *Why this design:* the problem statement is about follow-through; editing rows keeps the validated summary pipeline
  frozen.

### 7.13 Policy repository (CON-17, ADR-29)

- Upload PDF or DOCX (`POST /api/policies`, multipart). The original is kept under `data/policies/` addressed by its
  SHA-256 hash; the response is `202` while a background task extracts text (`pypdf`/`python-docx`, local,
  `server/policies/extract.py`) and indexes it.
- Versions are append-only. The **current** version is the newest one that is fully indexed, so the old version stays
  searchable until the new one is ready.
- `PolicyChunk`/`PolicyChunkVector` are separate tables (the transcript tables have mandatory meeting foreign keys)
  but use the same embedding model, vector-space guard, priority gate and citation rules.
- History Q&A takes `sources: meetings | policies | both` (default meetings). "Both" thresholds and caps each index
  separately, then interleaves them, instead of comparing scores across indexes.
- No OCR: an image-only PDF is kept, marked failed, and never searchable.

### 7.14 Periodic reports and multi-meeting summaries (CON-18, ADR-30)

- `/reports`: choose a UTC date range; `GET /api/reports/preview` and `…/download` produce a DOCX with each ended
  meeting (title, date, duration, participants, current summary or "No summary"/"Summary failed", action items) and
  figures (items opened, closed, cancelled in the range; open now).
- **No model call, no table, no stored file, no audit event:** it is a read of stored data, rendered on demand
  (`server/reports/service.py`, `server/export/report_renderer.py`). *Why:* aggregation of already-validated data has
  no new failure mode on stage.
- Over 50 meetings is refused (`400`), never truncated. An empty range is `409 report_empty`.
- The history page can tick several meetings and read their summaries one after another, summarize the missing ones
  one at a time, and download them as one DOCX (`GET /api/summaries/download`, `client/history_summaries.js`).

### 7.15 Emailing the minutes (ADR-33, ADR-34)

- Optional email on the join page, stored in its own table `ParticipantEmail` (migration 0011), **not** on
  `Participant`. *Why:* every participant view, dashboard push and audit payload stays free of addresses by
  construction.
- `POST /api/meetings/{id}/email` sends through Resend's REST API with stdlib `urllib` (`server/mail/resend.py`).
  Nothing is ever sent automatically; only the button sends.
- One message per recipient (no one sees another's address), about 0.6 s apart (Resend's rate limit), one retry on
  429/5xx, and one refused address does not stop the rest.
- Per-person sections (ADR-34): each recipient can get any of Summary, Action items and Transcript. The full set
  attaches the exported file itself; other sets are rendered in memory once per distinct set.
- The page shows masked addresses (it may be on a projector). The audit event `minutes_emailed` holds ids only.
- Configured only by environment (`mail.env`); the key is never in settings, logs or errors. A real send through the
  verified domain was confirmed on 2026-09-30, after fixing a sender-format bug that Resend rejected.

### 7.16 Frontend

**Files:** `client/`. Pages: home `/`, join `/join/{id}` (`index.html` + `app.js` + `mic_orb.js`), dashboard,
post-meeting `/meetings/{id}`, `/history`, `/action-items`, `/reports`, `/policies`, `/database` (a read-only inspector
of table counts and recent rows, to make persistence visible), `/dashboard` overview.

- No framework, no build step, no CDN; fonts and scripts are served locally, so the pages work offline.
- Shared light theme with indigo brand (`client/theme.css`); amber is reserved for "Needs review"; participant colours
  come from a fixed 12-key palette (ADR-25, `server/colors.py`) that excludes the brand colour and semantic colours.
- The join page shows a live microphone orb (Web Audio `AnalyserNode` on the sent stream) so participants can see
  they are being heard.
- **A real bug we hit:** an Android phone ran a cached old `app.js`. `Cache-Control: no-cache` was not enough for
  heuristically cached files, so every `/static` link now gets `?v=<file mtime>` (`server/routes.py:563-571`) and a
  changed file always has a new URL.
- Respects `prefers-reduced-motion`; motion uses transform/opacity only.

---

## 8. Cross-cutting principles in the code

| Principle | How the code enforces it |
|---|---|
| **One phone's failure never affects another** | Per-device sessions, locks, queues and pump tasks (`transport/peers.py`); per-device STT queues and round-robin (`scheduler.py`); per-device VAD state; bleed filter reads snapshots only; failures audited, not raised across devices |
| **The event loop never blocks** | DB writes on one worker thread (`db.run`); model calls via `run_in_executor`; audio sinks fed by bounded queues |
| **One record of what happened** | `audit.emit` is the only audit writer, validated against a catalog, in the same transaction as the change; dashboards and staleness derive from it |
| **Derived, not stored, state** | Staleness, "last changed", device history from `seq`; gauges are live-only |
| **Honesty over fluency** | Three Q&A outcomes; threshold before the model; citations from retrieval, not model text; low-confidence flags; no invented owners; no transcript truncation; failures stored with reasons |
| **Model produces data, code produces documents** | Summary is validated JSON; DOCX is a pure template function |
| **Local by default, cloud only by deliberate choice** | Models resolved from cache only; Gemini/Resend only via environment variables with a startup warning; no automatic fallback |
| **Config over code** | Models, thresholds, window sizes and limits in `config/convene.toml`, validated at load (`server/config.py`) |
| **Hooks, not coupling** | `on_transcribed_window`, post-write/correction hooks, `on_meeting_ended`; each hook isolated and time-limited (5 s), failures audited as `hook_failed` |
| **Privacy by structure** | No raw audio stored; audit payload keys exclude text; emails in a separate table; masked on screen; transcripts only printed to the console, never to tracked files |

---

## 9. Testing and verification

**Layout:** `tests/` (pytest, pytest-asyncio), `tests/js/` (Node test runner for the dashboard reducer),
`tests/e2e/`, `tests/support/synthetic_phone.py`, `tests/fixtures/` (synthetic TTS audio, transcripts, Q&A sets),
`tests/manual-test/` (operator checklists).

**Conventions:**

- Tests run offline without phones. Tests that need real model weights are marked `@pytest.mark.model` and excluded
  by default (`-m "not model"`).
- Real server under uvicorn on a loopback port, not a test client. *Why:* aiohttp's `TestServer` cancels handlers on
  disconnect, which the real server does not; a test harness that differs from production gave a false result in
  CON-01.
- A synthetic phone is a real aiortc peer that streams a tone or a WAV file through the real signaling and
  receive path.
- Mutation checks: deliberate breakages of the code to confirm tests catch them (20 in transport, 16 in storage, 23 in
  STT, 10 in the segmenter).
- Contract tests check the documentation itself (`tests/test_api_contract_docs.py`).
- In-process "offline" tests make every non-loopback socket and DNS lookup raise, then run a full model path and assert
  zero connection attempts.

**Commands:**

```bash
.venv/bin/python -m pytest -q -m "not model"   # default suite
.venv/bin/python -m pytest -q -m model         # needs provisioned models
node --test tests/js/                          # dashboard reducer
```

**Growth of the suite (from the logs):** 39 (CON-01) → 197 (CON-03) → 302 (CON-04) → 412 (CON-05) → 460 (CON-06) →
521 (CON-09) → 578 (CON-10) → 635 (CON-14) → 716 (CON-16) → 775 (CON-18) → 820 (email) → 824 (multi-summary).

**Current run (2026-09-30, development Mac):** `827 passed, 2 failed, 12 deselected` in 130 s. Both failures are in
`tests/test_api_contract_docs.py`: the `GET /api/overview` section of `docs/api.md` is missing its **Request** block,
and one example uses a `"…"` placeholder instead of a UUID. They are documentation-format failures in the contract
test, not code failures. They date from the host-overview commit, and are worth fixing before the review.

**Measurement scripts:** `scripts/measure_stt.py` (models and 1/2/5-phone pipeline load), `measure_embeddings.py`,
`measure_reasoning.py`, `measure_summary.py`, `calibrate_qa.py`, `verify_local_only.py` (samples TCP peers with `lsof`
and fails on any public peer), `provision_models.py`.

**What real devices have verified:** one real phone joined through the trusted hostname, its audio reached local
STT, and attributed lines appeared on the dashboard (2026-09-25). A real Resend email was sent (2026-09-30).

**What has NOT been verified on real devices:** multi-phone isolation, Wi-Fi drop and reconnect, screen lock, the
bleed filter, real Hindi speech accuracy, a 30 to 60 minute soak, the full offline demo rehearsal (CON-12 stability
gate), and opening the DOCX in Word, Pages and LibreOffice. Say so if asked.

---

## 10. Timeline

| Date (2026-09) | Milestone | Commit |
|---|---|---|
| 21 | Prototype and architecture docs; transport baseline tests and synthetic phone; API/event contracts; SQLite persistence and audit path | `d44ba68`, `a74e901`, `82fe841`, `24c8c24` |
| 22 | FastAPI transport replacing aiohttp; local STT with VAD speech segments | `6a95faa`, `feb4cbd` |
| 23 | Device attribution and manual correction; live dashboard | `a8854d4`, `e1264e2` |
| 25 | Trusted hotspot join; first real-device validation; STT language detection | `e80c116`, `63c3d9d`, `753cc6e` |
| 26 | Chunking and embeddings; indexer fixes; grounded live Q&A; summary and action items; UI redesign; Hinglish STT; DOCX export; demo rehearsal tooling | `69467f4` … `6d3bd4c` |
| 27 | History and cross-meeting Q&A, rename/delete; database inspector; action-item lifecycle | `d2c8e66`, `3ad324e`, `1ae32c0` |
| 28–30 | Policy repository; periodic reports; Gemini STT connector; bleed filter and pause/leave; email minutes; host overview; multi-meeting summaries | `ee13844` … `e6a71ee` |

---

## 11. What is not done, and known limitations

Be ready to state these plainly.

**Not built:**

- **Shared-device speaker classification (CON-13).** A phone declared as shared gets a generic "Speaker on Phone N"
  label, low confidence, correctable. The enrollment-based classifier (ADR-03) is designed but not implemented.
- **Load validation on real phones (CON-15).** Thresholds (VAD margin, bleed, Q&A similarity, attribution
  confidences) are from synthetic measurements.
- **Full offline rehearsal (CON-12).** Scripts and checklists exist; the three-run stability gate with several real
  phones has not been passed.

**Known limitations (documented, accepted):**

- Energy VAD cannot tell surging noise (music, a nearby conversation) from speech; it can create short false lines.
- A line appears about 1 to 2 s after the speaker pauses, not word by word.
- Bleed filter values are from synthetic tests; browsers may ignore `autoGainControl: false`.
- `device_id` is a bearer identifier; a LAN attacker could take over a phone's identity (visible in the audit).
- Q&A: answerable and near-miss questions overlap in similarity, so near-misses depend on the model declining.
- English embeddings match romanized Hindi only loosely, so Q&A over Hindi speech is weaker.
- Summary fails (rather than truncates) above 16,000 prompt tokens (~50 min of nonstop speech).
- The trusted-hostname mode needs weak internet for DNS; cloud STT and email need internet and void the offline claim
  for that run.
- A reloaded dashboard does not redisplay earlier Q&A answers (no list route).
- Some docs are behind the code: `AGENTS.md` still says `server/` is the DT-17 prototype, and
  `docs/feature-status.md` says cloud STT is not connected (it now is, ADR-31).

---

## 12. Likely questions with short answers

**Why not use a diarization model like pyannote?**
Diarizing mixed audio is the hard problem that makes other tools unreliable. With one phone per person, the
connection identifies the speaker for free and with near certainty. Diarization is only planned for the shared-phone
exception, and scoped to 2 or 3 enrolled voices on one phone. (ADR-02, ADR-03)

**What happens if two phones are close and both pick up one speaker?**
Each phone's VAD gate rejects weak audio first. Then the bleed filter compares loudness envelopes: if another phone
heard the same sound at least 6 dB louder, the quieter copy is dropped before STT and audited. If unsure, we keep
it; a wrong line can be corrected. (`server/pipeline/bleed.py:75`, ADR-32)

**Why WebRTC and not just send audio over WebSocket?**
WebRTC gives jitter buffering, packet-loss concealment and low latency over UDP. WebSocket is TCP, so one lost packet
stalls everything behind it. We validated WebRTC in our earlier prototype. (ADR-09)

**How does the phone get HTTPS without installing a certificate?**
A public hostname with a real certificate, whose DNS record the operator points at the laptop's private IP before the
session. The phone trusts the certificate and connects to the laptop locally. (ADR-20, `scripts/update_dns.py`)

**Why SQLite? Won't concurrent writes conflict?**
One laptop, one process. All writes go through one worker thread with `BEGIN IMMEDIATE`, so they serialize cleanly
and never block the event loop. WAL lets reads proceed. (`server/db.py`)

**How do you make sure the model doesn't hallucinate in Q&A?**
Three layers: (1) if no transcript chunk is similar enough (≥ 0.50), the model is not called; (2) the prompt says
answer only from excerpts, otherwise reply `NO_GROUNDING`; (3) citations come from retrieval, not model text. Tested:
0 of 36 fabricated answers. (`server/rag/qa.py:355`)

**What about prompt injection, e.g. someone saying "ignore previous instructions"?**
Transcript excerpts are fenced as data with an explicit instruction to ignore instructions inside them, and closing
fences inside the text are neutralized. It reduces the risk; no prompt-based defense is absolute. The threshold and
deterministic citations do not depend on the model obeying. (`build_messages`, `qa.py:89`)

**Why did you replace 1-second windows with speech segments?**
Each Whisper call costs ~0.5 s regardless of audio length, so 1 s windows overloaded the model at five phones (55%
dropped) and cut words in half. Segments cut at pauses fit five phones with zero drops and better accuracy. (ADR-19)

**What is the confidence score?**
Two separate numbers. `stt_confidence` from Whisper's log-probabilities, uncalibrated, only a ranking. And
`attribution_confidence`: 0.95 for a device, 0.2 for unresolved, 1.0 after a manual correction. The server decides
"low confidence" with one threshold (0.8) and sends a boolean. (ADR-17)

**If I correct a speaker, what happens to the summary?**
The utterance row changes, the original is preserved, and an `utterance_corrected` audit event records the full
before and after. The chunk is re-embedded with the new name. The summary and DOCX become stale (derived from audit
`seq`) and the page offers Regenerate. (`server/attribution/service.py:105`)

**How do you guarantee the DOCX is never broken?**
The model only produces validated JSON. A fixed `python-docx` template renders stored rows, written to a temp file,
fsynced, then atomically renamed. (ADR-12, `server/export/service.py`)

**What if the meeting is very long?**
The summary counts tokens with the model's tokenizer and fails clearly above 16,000 (~50 min nonstop) rather than
silently dropping the end. Map-then-merge is the documented next step. (ADR-23)

**Does it really work offline?**
In the default local mode, all models load from the local cache and the server makes no outbound connection;
in-process tests with all network access blocked passed for STT, Q&A and summary. A demo with Wi-Fi physically off,
and the trusted-hostname DNS lookup, are exceptions we state openly. Cloud STT and email are explicit opt-ins.

**Why is the dashboard reliable after a reconnect?**
Every event carries the audit `seq`. The dashboard buffers events, fetches a snapshot with `as_of_seq`, then replays
only events after it. No duplicates, no gaps. (ADR-18, `client/dashboard.js:254`)

**How does a phone reconnect after Wi-Fi drops?**
It keeps its `device_id` in `localStorage`, retries with backoff, sends `join` again, and builds a fresh peer
connection. The server recognizes the device, keeps the same participant and counts a reconnect. (aiortc cannot do
ICE restart, so we always build a fresh peer.)

**What happens if the server crashes mid-meeting?**
On restart, `reconcile_after_restart` marks devices that were connected as disconnected, and a pending summary is
marked failed. Phones reconnect under the same identity. Committed transcript lines are safe in WAL.

**How did you choose the models?**
By measurement on the target laptop, with scripts in `scripts/`. For example, distil-Whisper was neither faster nor
more accurate than turbo, and Llama beat Qwen on our grounded-answer test. The numbers are in `logs/`.

**Why the Gemini connector if you are local-first?**
Real-phone local STT quality was not good enough for the demo, so the user asked for an operator-selected cloud
option. It is off by default, switched only by environment variable, warns at startup, and never falls back
automatically. The adapter contract meant no other code changed. (ADR-31)

**What's the weakest part of the system?**
Honest answer: validation on real phones in a real room (bleed, noise, many phones, long meetings), and the energy
VAD's behaviour with surging noise. Shared-phone classification is also not built.

---

## 13. File map cheat sheet

| Concern | Files |
|---|---|
| Entry point, app factory | `server/app.py` (`create_app`, `main`), `scripts/start_demo.sh` |
| Wiring of all services | `server/runtime.py` (`Runtime.start`, `:135`) |
| Routes (REST, pages, WebSockets) | `server/routes.py`, `server/meetings.py` |
| Config and validation | `config/convene.toml`, `server/config.py` |
| Errors → HTTP codes | `server/errors.py` |
| DB, transactions, migrations | `server/db.py`, `server/migrations/0001…0011` |
| Audit | `server/audit.py`, `server/audit_catalog.py`, `server/repositories/audit_events.py` |
| Registry (meetings, devices, erase, restart) | `server/registry.py` |
| Transport | `server/transport/signaling.py`, `peers.py`, `sessions.py`, `audio.py` |
| Network, certificates, QR | `server/network.py`, `scripts/update_dns.py` |
| STT pipeline | `server/pipeline/pipeline.py`, `vad.py`, `segmenting.py`, `bleed.py`, `scheduler.py`, `priority.py` |
| STT adapters | `server/pipeline/adapter.py`, `mlx_whisper_adapter.py`, `gemini_adapter.py`, `romanize.py` |
| Attribution, labels, staleness | `server/attribution/service.py`, `labels.py`, `views.py`, `staleness.py` |
| Dashboard feed | `server/dashboard_hub.py`, `client/dashboard.js`, `client/dashboard_state.js` |
| RAG | `server/rag/chunker.py`, `indexer.py`, `embedding.py`, `vector_store.py`, `retrieval.py`, `reasoning.py`, `qa.py`, `citations.py` |
| Summary | `server/summary/service.py`, `prompt.py`, `schema.py`, `views.py` |
| Export | `server/export/docx_renderer.py`, `service.py`, `report_renderer.py` |
| Action items | `server/action_items/service.py`, `views.py`, `client/action_item_row.js` |
| Policies | `server/policies/service.py`, `extract.py` |
| Reports | `server/reports/service.py` |
| Email | `server/mail/resend.py`, `message.py`, `service.py` |
| Phone join page | `client/index.html`, `client/app.js`, `client/join_ui.js`, `client/mic_orb.js` |
| Other pages | `client/home.*`, `post_meeting.*`, `history.*`, `history_qa.js`, `history_summaries.js`, `action_items.*`, `reports.*`, `policies.*`, `policy.*`, `database.*`, `overview.*` |
| Decisions | `docs/decisions.md` (ADR-01 to ADR-34) |
| Work logs (evidence) | `logs/*.md` |
| Tests | `tests/`, `tests/js/`, `tests/e2e/`, `tests/support/synthetic_phone.py` |
