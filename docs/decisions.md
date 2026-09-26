# Architecture & Technology Decision Records

Each entry: decision, rationale, alternatives considered, tradeoffs, status. Read this before reopening any of these — they were not arrived at casually, and relitigating one live during the build costs time the project doesn't have.

---

### ADR-01: Single Python process, not split Node/Python services

**Decision:** The laptop server is one Python process — FastAPI for REST/WebSocket, `aiortc` for WebRTC media handling — not a Node.js transport layer talking to a separate Python AI sidecar.

**Rationale:** Every AI component in this system (STT, embeddings, speaker embeddings, local LLM) is Python-native. A split architecture would require an IPC boundary (HTTP/gRPC/queue) between the transport and AI layers purely to satisfy a language preference, adding a failure surface and debugging complexity with no functional benefit at this scale. `aiortc` is mature enough for 3–10 concurrent peer connections, which is comfortably within this project's target scale.

**Alternatives considered:** Node.js (`mediasoup`/native WebRTC libraries) for transport + a Python microservice for AI, communicating over HTTP or a local queue; a fully Node-based stack using a JS ML runtime for STT/LLM (rejected — JS ML tooling is far less mature than the Python ecosystem for this).

**Tradeoffs:** `aiortc` has a smaller community and less battle-testing at very high peer counts than Node's WebRTC ecosystem. Accepted — not a concern at 10-phone demo scale, and a single process is easier to reason about, log, and debug live during a demo.

**Status:** Locked.

---

### ADR-02: Device-per-speaker attribution, not acoustic diarization, as the primary mechanism

**Decision:** Speaker attribution for the common case (one phone per person) comes from which `RTCPeerConnection`/`participant_id` an audio window arrived on — not from a voice-diarization model. Acoustic diarization is used only as a secondary mechanism, scoped to a single device's stream, and only when that device is declared shared by more than one person.

**Rationale:** This is the project's core technical differentiator (see `project-context.md`). Diarizing an entire meeting from mixed audio is a well-known hard, error-prone problem; diarizing 2–3 known voices within one device's stream is a much smaller, better-conditioned problem. Preserving "device separation is speaker separation" as the default keeps the common case (which will be most demo groups) immune to the failure mode that damages trust in every other transcription tool.

**Alternatives considered:** Acoustic diarization across the full mixed/aggregate meeting audio (rejected — reintroduces the exact problem this architecture exists to avoid); no diarization at all, one generic label per device regardless of sharing (rejected — silently wrong when phones are shared, which the team expects will happen sometimes).

**Tradeoffs:** Does not solve cross-device audio bleed (one phone picking up a nearby phone's speaker) — mitigated separately via VAD/energy-gating (ADR-05), not diarization. Does not eliminate the need for a diarization model entirely — it's still needed for the shared-device case (ADR-03).

**Status:** Locked.

---

### ADR-03: Enrollment-based classification for shared devices, with generic-label + manual-correction fallback

**Decision:** When a device declares more than one speaker at join, each declared speaker records a short enrollment sample before the meeting starts. Runtime attribution on that device's stream classifies each window against the enrolled speaker embeddings (nearest-centroid, not unsupervised clustering). If enrollment didn't happen, or a window doesn't clear the similarity threshold against any enrolled speaker, the utterance is attributed to a generic label ("Speaker on Phone 2") and marked low-confidence rather than guessed at.

**Rationale:** Classifying against a known reference is materially more reliable than blind clustering, and fails safely (low-confidence generic label) rather than silently (confidently wrong name). This directly addresses the stated product risk: a wrong label is unacceptable, an honestly-uncertain one is fine.

**Alternatives considered:** Pure unsupervised clustering per device (rejected — no ground truth to anchor to, drifts over a long meeting); requiring 1:1 phone-to-person and disallowing sharing entirely (rejected — unrealistic for a real hackathon room where phone availability varies).

**Tradeoffs:** Requires extra join-flow friction (enrollment) for shared devices only. Enrollment quality depends on sample length/cleanliness — short or noisy samples may produce unusable embeddings, in which case the system must degrade to the generic-label path rather than fail. See `speaker-attribution.md` for the full state machine.

**Status:** Locked.

---

### ADR-04: Confidence score and manual correction are first-class data, not a UI afterthought

**Decision:** Every `Utterance` row carries an `attribution_method` and `confidence` field from the moment it's written, and every utterance is correctable via an explicit API endpoint, regardless of how it was originally attributed.

**Rationale:** The single biggest trust risk in this product is a wrong speaker label presented with false authority. Making confidence and correctability structural (part of the data model, not a UI nicety layered on later) means a wrong label is always visibly checkable and always a two-second fix, not a silent, permanent error baked into a summary or export.

**Alternatives considered:** Binary attribution with no confidence tracking (rejected — exactly the failure mode this whole architecture exists to avoid); correction as an unlogged in-place edit (rejected — loses the audit trail of what was originally said vs. corrected, which matters for defending the system's honesty under questioning).

**Tradeoffs:** Slightly more schema and UI work up front. Accepted — this is not optional given the product's own stated risk tolerance.

**Status:** Locked.

---

### ADR-05: Per-device VAD gating before windowing

**Decision:** Each device's audio stream passes through voice-activity detection before a window is sent to STT. A window is only processed if that stream's own signal clears an energy/VAD confidence threshold.

**Rationale:** Two independent benefits: (1) avoids wasting STT compute transcribing silence, materially reducing load at 5–10 concurrent phones; (2) is the primary defense against cross-device audio bleed — a phone picking up a nearby phone's speaker at low relative volume is less likely to clear its own gate than the phone that's actually being spoken into.

**Alternatives considered:** No gating, transcribe every window regardless of content (rejected — wastes compute and increases bleed-driven false transcriptions); gating based on absolute volume only rather than VAD (rejected — less robust across phone models/mic sensitivity).

**Tradeoffs:** VAD is not a perfect bleed filter — a loud nearby voice can still clear the gate. This is a mitigation, not a guarantee; residual bleed is still possible and is an accepted, documented limitation, not a silent gap.

**Status:** Locked.

---

### ADR-06: Local-first models, cloud as an explicit, manually-triggered fallback only

**Decision:** STT, embeddings, speaker embeddings, and the summarization/QA LLM all run locally by default (see `models.md`). A cloud fallback (Groq/Gemini free tier) exists behind the same adapter interface but is never invoked automatically or continuously — only via explicit configuration or a manual "use cloud for this" action.

**Rationale:** Continuous per-window STT and RAG-query traffic at 5–10 concurrent phones would exceed any free-tier's realistic rate limits quickly, and a rate-limit error mid-demo is an unacceptable failure mode. Local-first removes that risk from the critical path entirely, and is also a stronger pitch claim (zero internet dependency, zero per-meeting cost). Apple Silicon (M4 Pro, 24GB unified memory) comfortably fits the full local model set with headroom — see `models.md` for the budget.

**Alternatives considered:** Cloud-primary with local fallback (rejected — inverts the risk in exactly the wrong direction for a live demo); cloud-only (rejected outright — fails the offline requirement in `requirements.md`).

**Tradeoffs:** Local models are somewhat lower quality than the largest cloud models for summarization specifically. Accepted — quality is sufficient at meeting-summary scale, and reliability matters more than marginal quality for this use case.

**Status:** Locked.

---

### ADR-07: SQLite for all persistent application data

**Decision:** SQLite, one file, for meetings, participants, utterances, summaries, and all structured records.

**Rationale:** Zero-ops, embedded, matches the single-laptop deployment target exactly. No real concurrent multi-writer need at this scale (one server process, one meeting active at a time in the common case).

**Alternatives considered:** Postgres (rejected — operational overhead with no benefit at this scale); flat JSON files only (rejected — loses queryability needed for the dashboard, history view, and RAG chunk correlation).

**Tradeoffs:** Not suitable for a future multi-tenant/production deployment. Explicitly deferred, not a present concern (see `requirements.md` non-goals).

**Status:** Locked.

---

### ADR-08: `sqlite-vec` for the vector store, not a separate vector database

**Decision:** Transcript-chunk embeddings for RAG live in `sqlite-vec`, in the same SQLite file as the rest of the application data, rather than a standalone vector database (e.g. Chroma) as a separate process/store.

**Rationale:** Keeps the entire persistence layer to one file, one backup unit, one thing to reason about during a live demo — meaningfully simpler ops story than running and coordinating two data stores for a project this size.

**Alternatives considered:** Chroma (rejected — a fine tool, but an unnecessary second moving part when `sqlite-vec` covers the required scale entirely; reconsider only if chunk volume or query patterns genuinely outgrow it, which is not expected at hackathon corpus size).

**Tradeoffs:** Less headroom at very large corpus scale than a dedicated vector database. Not relevant at demo scale (a handful of meetings, thousands of chunks at most).

**Status:** Locked.

---

### ADR-09: WebRTC media transport, WebSocket signaling only (carried from DT-17)

**Decision:** Audio is transported over `RTCPeerConnection` media channels; WebSocket is used only to exchange signaling messages (join, SDP offer/answer, ICE candidates, reconnect control). No custom audio-over-WebSocket path.

**Rationale:** Validated in the DT-17 prototype phase — WebRTC's jitter handling, packet-loss concealment, and low-latency design are a better fit for live speech than a WebSocket/MediaRecorder-chunk approach, which suffers from imprecise chunk timing and TCP head-of-line blocking under loss. See the original `transport.md` rationale, restated here for completeness.

**Alternatives considered:** WebSocket + `MediaRecorder` blob transport (viable fallback if WebRTC ever proves problematic on a target browser, not pursued unless that happens).

**Tradeoffs:** None significant at this scale; already validated by the prototype phase.

**Status:** Locked (inherited).

---

### ADR-10: No STUN/TURN, no public signaling, no accounts

**Decision:** All devices are assumed to be on one local Wi-Fi network. No public STUN/TURN server, no cloud signaling relay, no user accounts or authentication system. A meeting ID (and the join link/QR code carrying it) is the sole access boundary.

**Rationale:** Matches the offline-first requirement exactly, and building any of this would be solving a problem (public internet NAT traversal, multi-tenant auth) this project doesn't have.

**Alternatives considered:** None seriously — this follows directly from the offline, single-room, single-laptop scope.

**Tradeoffs:** Not suitable for remote/distributed meetings. Explicitly out of scope (see `requirements.md`).

**Status:** Locked.

---

### ADR-11: Explicit-invocation RAG, never auto-injected

**Decision:** Retrieval (live-meeting Q&A or cross-meeting history search) runs only when explicitly triggered by a user action (asking a question) — never automatically prepended to any other operation, and never run continuously in the background.

**Rationale:** Keeps retrieval a visible, auditable, deliberate action rather than invisible machinery, which matters both for demo clarity ("watch, I'm asking it a question now") and for controlling compute load on a single local machine that's also running STT continuously.

**Alternatives considered:** Always-on background retrieval feeding continuous context into some other process (rejected — no other process needs it, and it would add load with no benefit).

**Tradeoffs:** None significant.

**Status:** Locked.

---

### ADR-12: Deterministic export rendering — the LLM produces data, code produces the document

**Decision:** The summarization LLM outputs structured data (summary text, an action-item list) via a constrained JSON response. A separate, deterministic step (`python-docx`) renders that data into the final DOCX file using a fixed template. The model never authors document formatting or markup directly.

**Rationale:** Reliability — a model cannot produce a malformed or broken document live in front of judges if it never touches formatting at all. Also makes the export format trivially easy to change (edit the template, not the prompt).

**Alternatives considered:** Having the model generate formatted document content (e.g. Markdown-to-DOCX) directly (rejected — less reliable, harder to guarantee consistent structure).

**Tradeoffs:** Less flexible output formatting than a fully model-driven approach. Accepted — a fixed, professional-looking template is exactly what's wanted for meeting minutes.

**Status:** Locked.

---

### ADR-13: One audit/event log as the single source of truth for observability

**Decision:** A single `AuditEvent` table records every event of interest (connections, reconnects, STT windows processed, attribution decisions, corrections, RAG queries, summarization runs, exports). The live dashboard is a filtered, real-time view over this same stream — not a separately maintained log.

**Rationale:** Avoids drift between what the live UI shows during the demo and what actually happened, which matters both for debugging during the build and for answering "how do we know that's accurate" under judge questioning.

**Alternatives considered:** Separate ad-hoc logging per component (rejected — multiple sources of truth that can disagree is a real risk, not a hypothetical one, in a system built under time pressure).

**Tradeoffs:** None significant.

**Status:** Locked.

---

### ADR-14: Resource-type indirection for all models

**Decision:** Application code never references a model name directly. Capabilities (STT, embedding, speaker-embedding, reasoning) request a resource type; a small configuration file maps each resource type to a concrete model + runtime. See `models.md`.

**Rationale:** Swapping a model (e.g. a faster STT model if load-testing shows the current one can't keep up) becomes a config edit, not an application-code change — directly useful given the real risk of needing to retune model choices close to demo day.

**Alternatives considered:** Hardcoding model identifiers in the STT/summarization/RAG code paths directly (rejected — makes late tuning riskier and slower).

**Tradeoffs:** A small amount of indirection for a meaningful amount of flexibility. No significant downside at this scale.

**Status:** Locked.

---

## Contract decisions made by CON-02

ADR-15 to ADR-18 finalize the API and event contracts (`api.md`, `transport.md`, `data-model.md`). They are **Proposed**: they follow the recommended resolutions in the CON-02 work order, they are already written into those documents so that CON-03 to CON-11 can build on them, and each needs project-lead confirmation before it is treated as locked. If one is rejected, the ADR and the documents it names are amended together. Gap numbers (G1 to G20, X1 to X6) refer to the ledger in `logs/contracts.md`.

---

### ADR-15: Meeting access, device identity, and Q&A result semantics

**Decision:**
- **Meeting ID (G1).** The meeting UUID is the stored key, the URL component, and the sole access boundary (ADR-10). No short join code is added. The join URL is `https://<lan-address>:<port>/join/<meeting_id>` and the QR encodes only that URL.
- **Impersonation (G5).** `device_id` is a bearer identifier with no secret. Any LAN client that learns it can `join` as that device and displace its connection. This limitation is accepted, not solved.
- **Q&A outcomes (G8).** `POST …/qa` returns HTTP 200 with the persisted `QAQuery` for `answered`, `no_grounding` and `failed`. The error envelope is only for request-level errors.
- **Q&A history after reload (G18).** No route lists past `QAQuery` rows in the MVP; a reloaded dashboard does not redisplay earlier answers.
- **Empty index (X5).** A meeting with no indexed chunks returns `no_grounding` when the index is healthy and `failed` when the embedding model or vector store cannot be used.

**Rationale:** A UUID in the URL is already unguessable and needs no new field; a QR of about 70 characters is easily scanned. A per-device secret would need a stored hash (a schema change) or in-memory tokens that break reconnection after a server restart, and the threat it addresses (someone in the same room deliberately hijacking a phone) is outside this project's model: no accounts, one room, correctable labels. The hijack stays visible after the fact through `device_reconnected` audit events and the reconnect counter. Returning `no_grounding` and `failed` as results keeps "the system does not know" and "the system is broken" distinguishable in one code path (`rag-and-qa.md`), which the honesty requirement depends on. Answers are stored, so omitting the list route loses only convenience.

**Alternatives considered:** A short join code (rejected: a second identifier and lookup for a marginal QR-density gain); a per-device secret (deferred: revisit if the demo environment has untrusted participants); HTTP error statuses for `no_grounding`/`failed` (rejected: conflates a result with a request fault and invites string matching); a `GET …/qa` list route (deferred: cheap to add later without changing any shape).

**Tradeoffs:** A malicious LAN participant can hijack a device silently in the moment; a reload mid-demo loses the on-screen Q&A history. The empty-index rule refines `rag-and-qa.md`, which listed "an empty vector store" under `failed`; it is read here as a store that cannot be queried, not an empty meeting.

**Status:** Proposed.

---

### ADR-16: Signaling contract

**Decision:**
- **Registration order (G3).** REST `POST …/devices` registers the device and creates its rows; the WebSocket `join` only attaches a connection to a registered device and is rejected otherwise.
- **Device ID (G4).** The phone generates a UUID `device_id` and persists it in `localStorage` under `convene:<meeting_id>:device_id`; the server issues `participant_id`.
- **Messages (G6).** Client to server: `join`, `offer` (optionally `ice_restart`), `leave`. Server to client: `joined`, `answer`, `error`, `meeting_ended`. There is no separate `reconnect` message and no `ice-candidate` message: one `join` serves first attach and reconnect (the server knows which), and ICE is non-trickle.
- **Casing.** All field names are `snake_case`, replacing the camelCase examples in the earlier `transport.md`.
- **Errors.** A malformed or invalid message gets an `error` reply, and the offending device's socket and peer are reset; other devices are untouched.
- **Lifetimes.** The signaling socket and the peer connection have separate lifetimes. Closing the socket alone does not change device status or close the peer; device status follows the peer connection state.

**Rationale:** Registration over REST gives one place that creates identity and lets the dashboard show a device before it connects. One `join` removes the case where the client and server disagree about whether a connection is a reconnect. Non-trickle ICE is what the DT-17 prototype used, needs no candidate plumbing, and is sufficient with no ICE servers (only host candidates). `localStorage` lets a closed and reopened tab resume the same identity, which `sessionStorage` (the prototype's choice) does not. One casing across REST, WebSocket, and the data model removes a class of mapping bugs. Keeping the peer alive across a socket loss is what makes the documented "ICE restart first" recovery possible.

**Alternatives considered:** WebSocket `join` creating the device (rejected: two registration paths); separate `join` and `reconnect` messages (rejected: redundant, and the server must not trust the client's claim); trickle ICE (deferred: more messages and ordering cases for no benefit on one LAN); keeping camelCase for signaling (rejected: two casings in one API); tearing the peer down on socket close as the prototype did (rejected: prevents ICE restart and turns a brief signaling drop into an audio drop).

**Tradeoffs:** The peer-lifetime change departs from the prototype and is unproven on real phones until CON-04 re-runs checks R3 and R4 (`logs/transport.md`). Non-trickle relies on the phone waiting for ICE gathering to finish, which the prototype capped at a few seconds. A fatal error resets a device's connection, so a spurious malformed message costs that device a reconnect.

**Status:** Proposed.

---

### ADR-17: Correction semantics, and server-owned confidence

**Decision:**
- **Body (G10).** A correction supplies exactly one of `participant_id` (any participant of the same meeting, including on another device) or `display_name` (reuse the unique participant with that exact name in the meeting, else create one on the utterance's device; more than one match is a conflict).
- **First original wins.** `Utterance.original_participant_id` keeps the participant from the first correction only. Each correction's audit payload records the full "from" state (participant, method, confidence, prior `corrected`) and the "to" state.
- **Confirm.** Correcting to the participant already assigned is allowed and means confirm: it sets `manual_correction` and confidence 1.0 and records `changed: false`. A repeat that changes nothing writes nothing.
- **Threshold (G20).** The low-confidence threshold is server configuration. The server sends `low_confidence` as a boolean (true also for `generic_unresolved`); clients never compare confidence numbers.

**Rationale:** Naming a generic speaker needs a target that may not be a participant yet, hence `display_name`. Keeping only the first original on the row and everything else in the audit stream avoids a schema change while preserving the complete history (ADR-04, ADR-13). Confirm is the fastest way to clear a low-confidence marker that turned out to be right, and it is still recorded. A single server-side threshold means the dashboard, summary, and export cannot disagree about what "low confidence" means.

**Alternatives considered:** Rejecting a same-participant correction (rejected: it forces a needless reassign-and-back to clear a marker); storing every prior state on the utterance row (rejected: schema growth duplicating the audit stream); a client-side threshold (rejected: business logic in the frontend, `frontend.md`).

**Tradeoffs:** `corrected = 1` with a null `original_participant_id` means "started unresolved", which readers must know. A confirmation is recorded as a correction, which slightly inflates correction counts.

**Status:** Locked. Accepted by the project lead for CON-06 on 2026-09-23. The initial configured values are
device confidence `0.95`, unresolved confidence `0.2`, and low-confidence threshold `0.8`; CON-15 tunes them
from real-phone evidence.

---

### ADR-18: Derived state, resynchronization, and schema additions

**Decision:**
- **Ordering and resync (G9, G16).** `AuditEvent` gains an integer `seq`, unique and strictly increasing, assigned by the single `emit` path in the same transaction. Every durable dashboard event carries the `seq` of its audit event; snapshots carry `as_of_seq`. Clients subscribe, buffer, fetch, and discard buffered events at or below the snapshot's `as_of_seq`. Transcript reads accept `after_seq`.
- **Gauges versus audit (G12).** Durable state (device status, reconnect counts, meeting status, staleness) is derived from the audit stream; last-audio-age, audio duration, STT backlog and drops are live in-memory gauges, never persisted and never used to answer what happened.
- **Staleness.** A summary or export is stale when a newer `utterance_created` or `utterance_corrected` exists than the `input_as_of_seq` in its own audit event. No stored flag.
- **Failure state (G13, G14).** `Summary` and `Export` gain `status` (`pending`, `ready`, `failed`) and a nullable `error_message`; `summary_text` and `storage_path` become nullable. A failed attempt never replaces the current `ready` result.
- **Flows (G15).** `POST …/end` returns 202 (200 if already ended) with `summary_pending`; `summarize` creates a new `Summary`; export is rendered automatically after a successful summary and re-rendered on demand when stale, and never served stale silently.
- **Audit catalog (G11) and events (G7).** The complete audit catalog is in `data-model.md`; the dashboard gains `meeting_status`, `device_status`, `device_gauges`, `utterance_updated`, `summary_failed`, `export_ready`, `export_failed`, and `error`.

**Rationale:** A resync needs a total order that survives restarts; the audit stream already is the ordered record, so its position is the natural cursor. Deriving staleness from the same stream means nothing can disagree with it (ADR-13). The failure states are already required by `architecture.md` ("marked failed") but had nowhere to be stored.

**Alternatives considered:** A per-meeting in-memory counter (rejected: resets on restart, breaking resync); client re-fetch on every event (rejected: wasteful and racy); stored `stale` flags (rejected: can drift from the stream); a separate failure table (rejected: a status column is simpler).

**Tradeoffs:** Three schema additions (`AuditEvent.seq`, `Summary` and `Export` status and error). `seq` is not contiguous per meeting, so clients must not treat gaps as loss. Ephemeral events cannot be replayed after a reconnect; the next snapshot supplies current values instead.

**Status:** Proposed.


---

## Decision made during CON-05

### ADR-19: STT input is VAD-bounded speech segments, not ~1 s windows

**Decision:** Each device's audio is sent to STT as one *segment* per stretch of speech: it starts when the device's VAD hears speech (with a short pre-roll), ends after a pause (600 ms by default) with a short tail, and is capped at 8 s, where it is cut at the quietest nearby frame. A voiced burst under 300 ms is discarded. Fixed windows (about 1 s, the earlier design in `stt-pipeline.md`) remain available as `[pipeline] segmentation = "fixed"`.

**Rationale:** Measured on the reference laptop with `whisper-large-v3-turbo`, one model call costs about 0.5 s whether the audio is 1 s or 8 s, so one worker sustains only about 2 calls per second. Fixed 1 s windows therefore overloaded the model at five phones (55% of windows dropped, about 4 s of lag when all five talked) and cut words in half (word error rate 0.156 to 0.219 on realistic streams, with fragments such as "very tough. height."). Segments sent 3 to 6 times fewer calls, produced one line per sentence, and had word error rate 0.000 on the same streams. One line per stretch of speech is also the right granularity for `Utterance` rows, live Q&A citations and the exported minutes.

**Alternatives considered:** longer fixed windows (3 s: word error rate 0.031 to 0.078, fits five phones, but still cuts words at arbitrary points and shows fragments; kept as the fallback); a smaller model (`whisper-small` about 5 calls/s, `base` about 25: keeps 1 s windows but trades accuracy that clean synthetic speech cannot show, and does not fix mid-word cuts); more workers (inference is GPU-bound: 4 workers gave 2.06 calls/s against 1.94 for one).

**Tradeoffs:** a line appears about 1.2 s after the speaker stops (pause detection plus the model call) instead of streaming every second; correctness now depends on the energy VAD detecting pauses well, which was only measured on synthetic material, so a noisy room can merge two people's speech into one segment or fail to end one (the 8 s cap bounds it). The VAD cannot tell surging noise from speech, so such noise can still yield false lines. Changing this document's fixed ~1 s windows was approved by the project lead (2026-09-22); real-phone validation is still owed (`manual-tests.md`).

**Status:** Accepted by the project lead; pending validation on real phones.

---

### ADR-20: Trusted public hostname with operator-run dynamic DNS preflight

**Decision:** Participant join URLs use one configured public hostname, covered by a publicly trusted ACME
DNS-01 certificate. Before each hotspot session, the operator explicitly runs `scripts/update_dns.py` to set the
DNS-only Cloudflare A record to the laptop's current private hotspot address. The server receives only the public
hostname and certificate/key paths; it never reads Cloudflare credentials or calls a CA/DNS-provider API. It checks
certificate coverage, expiry, and local hostname resolution at startup. The hotspot needs weak-but-working internet
for the preflight and the phone's initial DNS lookup; WebRTC, signaling, page assets, STT, and dashboard traffic stay
on the local network.

**Rationale:** Mobile browsers require trusted HTTPS for microphone access. A public certificate removes the
per-phone CA/profile installation that makes a QR-based join unusable, while an explicit preflight keeps a remote
DNS mutation out of the live server process and makes the operator action visible and recoverable.

**Alternatives considered:** `mkcert` on every phone (rejected for participant UX); certificate-warning bypasses
and browser flags (rejected as insecure and unreliable); public tunnels or relays (rejected because audio/signaling
must remain local); server-automatic Cloudflare updates (rejected because the server must not hold or use DNS
provider credentials); a travel router with local DNS (deferred because the chosen hotspot flow accepts weak DNS
connectivity).

**Tradeoffs:** This is not a no-internet deployment: DNS rebinding protection, Private DNS/Relay, stale caches, or
a venue with no usable connectivity can prevent initial hostname resolution. These are measured on real Android and
iOS phones before relying on the flow. The non-standard `:8443` port remains in QR URLs.

**Status:** Accepted for CON-04B implementation; pending real-phone validation.

---

### ADR-21: Auto-detect STT language; do not add translation

**Decision:** The default local STT setting is `language = "auto"`. The adapter passes this as `None` to
`mlx-whisper`, invoking its built-in multilingual language detection for each speech segment. An operator may set an
explicit Whisper language code, such as `en`, for a known monolingual run. Convene does not translate transcripts,
provide language-selection UI, or make any multi-language product claim.

**Rationale:** The first real-device run exposed Hindi/Hinglish speech while the production configuration forced
English. The selected Whisper model is multilingual, so auto-detection is the smallest local, offline change that
lets the configured model attempt to transcribe the spoken language without changing transport, attribution, or API
contracts.

**Alternatives considered:** Keep English forced (rejected: it knowingly degrades non-English speech); force Hindi
(rejected: it harms English and mixed meetings); switch STT runtimes or use an Ollama text model (rejected: neither
is necessary to invoke the existing local multilingual STT capability).

**Tradeoffs:** Detection happens per segment, so short or code-switched segments can select the wrong language.
Hindi/Hinglish quality, latency, and interaction with the VAD must be measured on real phones before a demo claim.

**Status:** Superseded by ADR-24 on 2026-09-26 (auto-detection produced Spanish text for Hindi speech on a real phone).

---

### ADR-22: Live Q&A outcome reasons, as-of rule, and the relevance threshold as the primary honesty guard

**Decision:**
- **Reasons.** Every `no_grounding` or `failed` Q&A result carries a `reason` in the response and the `qa_query`
  audit payload (`rag-and-qa.md` outcome table), not on the `QAQuery` row. `error.code` keeps the two documented
  codes (`retrieval_failed`, `generation_failed`).
- **Empty index (refines ADR-15, X5).** Lines with nothing indexed yet are `no_grounding` (`not_indexed_yet`) while
  the index is healthy, and `failed` (`index_unavailable`) when a chunk has failed with nothing ready or indexing
  is more than `[qa].index_stale_s` behind.
- **As-of rule.** A chunk is eligible if its first line was written at or before the question.
- **Honesty guard.** Below `[qa].min_similarity` the model is not called. The prompt's `NO_GROUNDING` instruction
  is a second guard, not the first.
- **Citations** are the chunks given to the model as evidence.
- **Residency.** The reasoning model loads at startup and stays resident.

**Rationale:** A just-started meeting is not broken, and a broken index must not look like "nothing relevant",
so the reason travels with every result while the stored row stays as documented. A per-line as-of filter
inside a chunk would split citation units for a millisecond-scale edge case. A local 7–8B model will sometimes
answer from general knowledge despite instructions, so the guard that does not depend on it has to be retrieval.
Deterministic citations cannot be invented by the model. A cold load at the first question would stall the demo.

**Alternatives considered:** A new `QAQuery.reason` column (deferred: a schema change for information the audit
stream already holds); per-utterance as-of filtering (rejected, above); model-selected citations (rejected:
parsing model text is exactly what `api.md` forbids); loading the reasoning model per call (rejected: seconds of
cold start on stage).

**Tradeoffs:** At 400-token chunks, answerable and near-miss questions overlap in similarity, so near-misses
reach the model and honesty on them depends on its instruction following (measured in `logs/qa.md`). A resident
model holds several GB for the whole meeting (`models.md`).

**Status:** Accepted (CON-09).

---

## Decision made during CON-10

### ADR-23: Summary length limit, end-only drain, and summary failure details

**Decision:**
- **Long transcripts.** The whole transcript goes to the model in one call or not at all. The prompt is counted
  with the model's tokenizer; above `[summary].max_input_tokens` (16,000, measured) the attempt fails with
  `transcript_too_long` and the model is not called. No truncation and no map-then-merge pass.
- **Drain only at meeting end.** The end trigger flushes partial segments and waits, bounded by
  `[summary].drain_timeout_s`, for queued STT and the attribution writes behind it. A manual trigger neither flushes
  nor waits.
- **Failure detail.** `summary_failed.error_code` is one of `summary_invalid_output`, `summary_generation_failed`,
  `transcript_too_long` and `transcript_empty`. `summary_generated` and `summary_failed` gain `attempts`, `duration_ms`
  and `drain_timed_out`. `Summary.generated_at` is null while `pending`. A `pending` row left by a stopped server is
  failed at startup.
- **Parsing and owners.** Unwrap one code fence or prose around one JSON object, ignore extra keys, read a blank
  owner as null, and reject everything else without repair. Owners map by case-insensitive exact display-name
  match; an ambiguous or unknown name, or a generic label, is null.
- **One attempt per meeting.** A second trigger is `409 summary_in_progress`; the end trigger does not queue behind a
  running manual attempt.

**Rationale:** Measured on the reference laptop (`logs/summary-export.md`), memory grows slowly with context (5.5 GB
at 2k tokens, 8.7 GB at 27k) but time does not: prefill runs at 160–290 tokens per second, so 90 minutes of nonstop
talk took over four minutes and overran the output budget. 16,000 tokens covers about 50 minutes of nonstop speech
and far more of a normal meeting in about 100 s, many times the demo's length. A clear failure is honest, where
truncation would silently drop the end of the meeting and map-then-merge adds a second prompt and failure mode that
the demo does not need. Flushing on a live meeting would cut off a sentence being spoken. The extra audit keys make
the retry rate, time to summary and drain timeouts visible from the audit stream (ADR-13).

**Alternatives considered:** A 128k context (rejected: minutes of prefill, and a live meeting's STT would stall for
the duration); truncating to the latest part (rejected: silent loss); map-then-merge (deferred until a real meeting
needs it); retrying model errors (rejected: a failing model fails again, and the contract retries parse failures
only); queueing a meeting-end attempt behind a running manual one (rejected: the staleness notice already covers it).

**Tradeoffs:** A meeting longer than the limit gets no summary until the limit is raised or map-then-merge is added.
A long summary holds the reasoning model, so a live question waits behind it.

**Status:** Proposed (CON-10), pending project-lead confirmation.

---

### ADR-24: English and romanized Hindi only; no per-segment language detection

**Decision:** `[models.stt].languages` is `["en", "hi"]` (default) or `["en"]`; no other value is accepted. Every
segment is decoded by Whisper in English mode. When Hindi is allowed, the decode carries a short code-mixed
`hindi_prompt` ("Okay, so the meeting kal hai. Haan, main dekh lunga. Theek hai."), which makes Whisper write Hindi
speech in Latin letters, the way Hinglish is typed ("Tum loog kya soch rahe ho? Kya yeh plan thik hai?"). This is
transliteration by the STT model, not translation; Convene still does not translate.

**Rationale:** On a real phone, `language = "auto"` turned Hindi speech into Spanish ("Gracias", "¿Qué agarró?").
Participants read and type Hindi in Latin letters, and the summary, Q&A and DOCX pipeline is English-first. Measured
on the reference laptop (`logs/stt.md`): with the prompt, five Hindi clips came out as readable romanized Hindi with
English loanwords kept ("launch", "report", "budget"); seven English clips were word-for-word identical with and
without the prompt; a Spanish clip could no longer come out as Spanish. Without the prompt, English mode translates
Hindi, badly ("What do you think? What do you think?").

**Alternatives considered:** Restrict Whisper's detector to en/hi and decode Hindi as `hi` (rejected: Devanagari
output, which the user does not want). Detect en/hi first and prompt only Hindi segments (rejected: the detector is
a second encoder pass, measured at about 480 ms per segment, which doubled STT time for no measured gain in English
accuracy). Transliterate Devanagari afterwards with rules or the local LLM (rejected: extra latency and a second
error source, for spellings no better than Whisper's).

**Tradeoffs:** Romanized spelling is Whisper's, not standardized ("hoogi", "chaheye"). Measured only on synthetic
text-to-speech voices; real Hindi and code-switched speech on phones must be checked. A different or smaller STT
model may not follow the prompt. English embeddings (`bge-small-en`) match romanized Hindi only loosely, so Q&A over
Hindi speech is weaker than over English.

**Status:** Requested by the project lead on 2026-09-26; synthetic-speech measurement done, real-phone check open.


---

### ADR-25: Participant colours from a fixed palette of 12

**Decision:** Each participant has a `color`: one of 12 palette keys (`lime`, `green`, `teal`, `cyan`, `sky`, `azure`,
`violet`, `plum`, `magenta`, `pink`, `slate`, `charcoal`; `server/colors.py`). A phone may choose one at join from a
swatch picker that greys out colours already used in the meeting (`GET /api/meetings/{id}/colors`); a registration
asking for a used colour gets `409 color_taken`. With no choice, the server assigns a random unused colour; after all
12 are used, a random least-used one. A participant created by a correction ("name this speaker") is assigned one
the same way. A replayed registration (the same phone rejoining) returns the stored colour. The server stores only the
key; the client maps keys to hex values. The Convene brand colour (indigo) is never in the palette: it belongs to the
product's own mascot, logo and actions.

**Rationale:** The transcript identifies speakers by a recoloured mascot avatar, WhatsApp-style. Keys (not hex) keep
the palette tunable in one CSS file. A fixed palette guarantees legible, mutually distinct colours on the light
theme and keeps them away from the semantic colours (review amber, error red, brand indigo); a free colour wheel
would allow near-duplicates, unreadable pastels and colours that read as "needs review" or "error".

**Alternatives considered:** Free colour wheel (rejected above); colour derived from a hash of the participant id
(rejected: no choice, and collisions within a meeting); a unique index in SQLite (rejected: a 13th participant must
still get a colour).

**Tradeoffs:** Uniqueness holds only while fewer than 13 participants exist; beyond that the name disambiguates.
Rows from before migration 0007 have a null colour; the client derives a stable one from the participant id.

**Status:** Requested by the project lead on 2026-09-26.

---

### ADR-26: Q&A answers carry no inline sources; sources sit behind one toggle

**Decision:** The Q&A prompt asks for at most three sentences naming the speaker for each fact, with no times or
brackets. `clean_answer` removes any inline reference the model still writes (`(Name, 00:12:03)`, `[Name, 00:12:03]`,
`(excerpt 2)`) before the answer is stored, unless that would leave nothing. The dashboard shows the answer as plain
text and its citations behind a collapsed **Sources (n)** toggle; citations are unchanged (the retrieved chunks,
never parsed from the model's text), and each still scrolls to and highlights the cited lines.

**Rationale:** On a real run the answer repeated the excerpts with a reference after every fragment and the citation
box repeated them again; the panel read as noise. Sources stay one click away for anyone checking grounding.

**Alternatives considered:** A longer prompt spelling out the style (rejected: measured on the reference laptop, it
diluted the `NO_GROUNDING` rule; the honesty test then saw "There is no information about…" answers instead of a
decline). UI-only cleanup (rejected: the stored answer, which later exports may show, would keep the clutter).

**Tradeoffs:** The regex can remove a genuine parenthesised time with a comma, such as "(launch, 10:30)"; a bare
time in a sentence is kept. Real answers must be spot-checked on the demo transcript.

**Status:** Requested by the project lead on 2026-09-26; the real-model honesty test passes with the new prompt.

