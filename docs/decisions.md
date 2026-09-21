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
