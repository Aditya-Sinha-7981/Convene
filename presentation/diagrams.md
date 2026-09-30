# Convene — presentation kit

Mermaid diagrams, key numbers and a slide outline for presenting Convene. Every figure comes from `logs/`,
`docs/models.md` or `docs/decisions.md`. Paste any ```` ```mermaid ```` block into
[mermaid.live](https://mermaid.live) to export PNG/SVG for slides. GitHub and VS Code render them in place.

Status note for the presenter: the features below are **built and covered by automated tests**. Real-phone checks
so far: one phone, end to end (2026-09-25), and one real email send (2026-09-30). The multi-phone, offline
Wi-Fi-off and three-run rehearsal checks (CON-12, CON-15) are **not run yet**. Section 12 lists what is verified.
Don't claim more than that on stage.

---

## 1. The pitch in one slide

> **Every phone in the room becomes that person's microphone.** One laptop transcribes each phone on its own,
> knows who said what without guessing from voices, answers questions about the meeting as it happens, and
> gives you the minutes as a DOCX when you end it. Nothing leaves the room.

| Problem (from the brief) | Convene's answer |
|---|---|
| Minutes are written inconsistently | Structured summary + deterministic DOCX, the same template every time |
| "Who said that?" | Attribution by **device**, not by guessing from voices. Every line has a confidence score and can be corrected |
| Nobody can find what was decided | Live Q&A grounded in the transcript, with sources. It says "not discussed" rather than making something up |
| Commitments get lost | Action items you can edit across meetings (owner, due date, status, follow-up notes) |
| Policies are scattered and unversioned | Append-only policy repository that you can search with the same Q&A |
| No periodic reporting | Reports over a date range, built from stored data (no AI call) |
| Privacy | Runs on one laptop and a local Wi-Fi network, with local models. Cloud is only used if the operator turns it on |

---

## 2. System architecture

```mermaid
flowchart LR
    subgraph Room["Meeting room · local Wi-Fi, no Internet"]
        P1["📱 Phone 1<br/>Priya"]
        P2["📱 Phone 2<br/>Marcus"]
        P3["📱 Phone N<br/>…up to 10"]
    end

    subgraph Laptop["💻 One laptop · single Python process (FastAPI + aiortc)"]
        direction TB
        SIG["WebSocket signaling<br/>+ device registry"]
        RTC["WebRTC audio receiver<br/>one isolated session per phone"]
        PIPE["Audio pipeline<br/>bleed filter → VAD → speech segments"]
        STT["Local STT<br/>Whisper large-v3-turbo (MLX)"]
        ATT["Attribution<br/>device → participant + confidence"]
        AUD[("Audit / event stream<br/>single write path")]
        DB[("SQLite + sqlite-vec")]
        IDX["Chunker + embeddings<br/>BGE-small"]
        LLM["Reasoning model<br/>Llama 3.1 8B 4-bit (MLX)"]
        QA["Grounded Q&A"]
        SUM["Structured summary<br/>+ action items"]
        DOCX["Deterministic DOCX renderer"]
        HUB["Dashboard WebSocket hub"]
    end

    DASH["🖥️ Host dashboard<br/>transcript · health · Q&A · correction"]

    Room -- "HTTPS join page<br/>+ signaling" --> SIG
    Room == "Opus audio over WebRTC<br/>(no STUN/TURN)" ==> RTC
    RTC --> PIPE --> STT --> ATT --> DB
    ATT --> AUD
    DB --> IDX --> DB
    AUD --> HUB --> DASH
    DASH -- "explicit question" --> QA
    QA <--> DB
    QA --> LLM
    SUM --> LLM
    DB --> SUM --> DOCX
```

**Talking point:** it is one process on one laptop, with no microservices, no cloud and no accounts. WebRTC carries the
audio, a WebSocket carries control messages and live updates, and SQLite stores everything.

---

## 3. A meeting from start to finish

```mermaid
sequenceDiagram
    autonumber
    actor Host
    participant L as Laptop server
    actor Phone as Participant phone
    participant M as Local models

    Host->>L: Create meeting
    L-->>Host: Dashboard + QR code (join URL)
    Phone->>L: Scan QR → HTTPS join page
    Phone->>L: Name, colour, optional email → register device
    L-->>Phone: device_id (kept for reconnects)
    Phone->>L: WebRTC offer / answer over WebSocket
    Phone-)L: Live microphone audio (Opus)
    loop Every speech segment
        L->>M: Transcribe segment (Whisper, local)
        M-->>L: Text + stt_confidence
        L->>L: Attribute to the device's participant
        L-)Host: utterance_created (live transcript)
        L->>L: Chunk + embed for search (async)
    end
    Host->>L: "What did we decide about the budget?"
    L->>M: Retrieve → answer only from evidence
    L-->>Host: Answer + sources, or "no grounding"
    Host->>L: End meeting
    L->>L: Drain STT, flush chunks
    L->>M: Structured summary + action items (JSON)
    L-->>Host: Post-meeting view
    Host->>L: Download DOCX / email minutes
```

---

## 4. The audio pipeline (why it holds up with 5 phones)

```mermaid
flowchart LR
    A["WebRTC frames<br/>48 kHz Opus"] --> B["Resample<br/>16 kHz mono"]
    B --> C{"Cross-device<br/>bleed filter<br/>(ADR-32)"}
    C -- "louder on another phone" --> X1["Dropped<br/>(counted)"]
    C -- "own speaker" --> D{"Adaptive energy VAD<br/>rolling 10th percentile<br/>+ 9 dB margin"}
    D -- "silence / steady noise" --> X2["Gated out"]
    D -- speech --> E["Speech segmenter<br/>VAD-bounded segments<br/>(ADR-19)"]
    E --> F["Bounded fair scheduler<br/>per-device queues"]
    F -- "overload" --> X3["Drop + audit event<br/>(never silent)"]
    F --> G["Whisper large-v3-turbo<br/>MLX · local"]
    G --> H["Hallucination filter<br/>no_speech / logprob / compression"]
    H --> I["Utterance row<br/>+ live push"]
```

**The key change (ADR-19):** fixed ~1 s windows dropped **55%** of windows with 5 phones talking. Speech segments
cut at the pauses gave **zero drops** with 5 phones.

```mermaid
xychart-beta
    title "Windows dropped, 5 synthetic phones talking continuously"
    x-axis ["1 s windows", "2 s windows", "3 s windows", "Speech segments (ADR-19)"]
    y-axis "Dropped windows" 0 --> 300
    bar [274, 54, 2, 0]
```

---

## 5. Attribution: who said it

```mermaid
flowchart TD
    U["New speech segment<br/>from device D"] --> Q{"Is device D<br/>declared shared?"}
    Q -- "No (the normal case)" --> DEV["Participant = D's registered person<br/>method: device<br/>confidence: 0.95"]
    Q -- "Yes" --> ENR{"Voice matches an<br/>enrolled speaker?"}
    ENR -- "clear match" --> E1["method: enrolled<br/>confidence from similarity"]
    ENR -- "ambiguous" --> E2["method: generic_unresolved<br/>'Speaker on Phone 3'<br/>low confidence, flagged"]
    DEV & E1 & E2 --> ROW[("Utterance")]
    ROW --> FIX{"Host clicks the line<br/>and picks a person"}
    FIX --> C["method: manual_correction<br/>confidence: 1.0<br/>original_participant_id kept"]
    C --> AUDIT[("Audit event<br/>full original preserved")]
    C --> STALE["Summary & DOCX marked stale<br/>search chunk rebuilt"]
```

```mermaid
stateDiagram-v2
    [*] --> device: non-shared phone
    [*] --> enrolled: shared phone, voice matched
    [*] --> generic_unresolved: shared phone, ambiguous
    device --> manual_correction: host corrects
    enrolled --> manual_correction: host corrects
    generic_unresolved --> manual_correction: host corrects
    manual_correction --> manual_correction: corrected again
    note right of manual_correction
        confidence = 1.0
        original attribution kept
        in the utterance and the audit log
    end note
```

**Talking point:** diarization (guessing speakers from voices) is the weak part of most meeting tools. We avoid it:
one phone is one person, so we already know who is speaking. Shared phones are the only place that needs a guess, and the
guess comes with a confidence score that the host can correct.

---

## 6. Grounded Q&A: honest by design

```mermaid
flowchart TD
    Q["Host asks a question<br/>(only when asked, ADR-11)"] --> E["Embed question<br/>BGE-small, local"]
    E --> R["Retrieve chunks from this meeting<br/>as of the moment it was asked"]
    R --> I{"Index empty?"}
    I -- yes --> NG1["no_grounding<br/>'Nothing indexed yet'"]
    I -- no --> T{"Best chunk above the<br/>calibrated relevance threshold?"}
    T -- no --> NG2["no_grounding<br/>model is <b>never called</b>"]
    T -- yes --> L["Llama 3.1 8B<br/>prompt: transcript is untrusted evidence"]
    L --> OK{"Generated in time<br/>and well-formed?"}
    OK -- no --> F["failed<br/>(shown as an error, not an answer)"]
    OK -- yes --> A["answered<br/>+ citations from stored chunks<br/>(speaker · time)"]
```

| Measured on the reference laptop (M4 Pro, 24 GB) | Result |
|---|---|
| Grounded questions answered correctly | **36 / 36** |
| Answers made up for questions the meeting never covered | **0 / 36** |
| Honesty gate (72 questions) | Passed |
| Answer time, median / max | **3.9 s / 4.8 s** |
| Network sockets opened during Q&A | 0 (in-process network block) |

---

## 7. Summary and minutes

```mermaid
flowchart LR
    END["Meeting ends"] --> D["Drain STT queue<br/>flush search chunks"]
    D --> T["Corrected, labelled transcript"]
    T --> P["Constrained prompt<br/>→ JSON only"]
    P --> V{"Parses and<br/>validates?"}
    V -- no --> R["One stricter retry"]
    R --> V2{"Valid?"}
    V2 -- no --> FL["status: failed<br/>bad output is never stored"]
    V -- yes --> S[("Summary + ActionItems<br/>owners matched exactly to participants")]
    V2 -- yes --> S
    S --> DX["Deterministic DOCX renderer<br/>(code, not the LLM, ADR-12)"]
    S --> PM["Post-meeting page"]
    DX --> DL["Download"]
    DX --> EM["Email chosen parts<br/>to chosen people (ADR-33/34)"]
```

**Reliability measured:** 6 fixture meetings × 3 runs = 18 runs. Parse failures **0**, retries **0**, invented
owners **0**. At temperature 0 the three runs gave identical action items.

---

## 8. Data model (core entities)

```mermaid
erDiagram
    MEETING ||--o{ DEVICE : "has"
    MEETING ||--o{ PARTICIPANT : "has"
    DEVICE ||--|{ PARTICIPANT : "one (or several if shared)"
    PARTICIPANT ||--o| PARTICIPANT_EMAIL : "optional"
    PARTICIPANT ||--o{ SPEAKER_ENROLLMENT : "shared devices only"
    MEETING ||--o{ UTTERANCE : "transcript"
    DEVICE ||--o{ UTTERANCE : "captured by"
    PARTICIPANT ||--o{ UTTERANCE : "attributed to"
    MEETING ||--o{ TRANSCRIPT_CHUNK : "indexed as"
    MEETING ||--o{ QA_QUERY : "asked in"
    MEETING ||--o{ SUMMARY : "summarised by"
    SUMMARY ||--o{ ACTION_ITEM : "produces"
    ACTION_ITEM ||--o{ ACTION_ITEM_NOTE : "follow-ups"
    MEETING ||--o{ EXPORT : "rendered to"
    MEETING ||--o{ AUDIT_EVENT : "records"

    MEETING {
        uuid meeting_id PK
        text title
        text status "created | live | ended"
    }
    DEVICE {
        uuid device_id PK
        bool is_shared
        int reconnect_count
        text status
    }
    PARTICIPANT {
        uuid participant_id PK
        text display_name
        text color
    }
    UTTERANCE {
        uuid utterance_id PK
        text text
        real stt_confidence
        text attribution_method
        real attribution_confidence
        bool corrected
        uuid original_participant_id
    }
    QA_QUERY {
        uuid query_id PK
        text mode "live | history"
        text status "answered | no_grounding | failed"
        json cited_chunk_ids
    }
    SUMMARY {
        uuid summary_id PK
        text status "pending | ready | failed"
        text model_identifier
    }
    ACTION_ITEM {
        uuid action_item_id PK
        text status "open | done | cancelled"
        date due_date
        uuid owner_participant_id
    }
```

Policies are stored separately (`PolicyDocument` → `PolicyVersion`, append-only) with their own chunk and vector tables
in the same SQLite file (ADR-29).

---

## 9. Everything runs locally

```mermaid
flowchart TB
    subgraph Local["Default: all local on the laptop"]
        S1["stt → whisper-large-v3-turbo<br/>mlx-whisper · ~1.8 GB"]
        E1["embedding → bge-small-en-v1.5<br/>sentence-transformers · CPU"]
        R1["reasoning → Llama-3.1-8B-Instruct-4bit<br/>mlx-lm · ~5.5 GB · always loaded"]
    end
    CFG["config/convene.toml<br/>models chosen by resource type,<br/>pinned revisions (ADR-14)"] --> Local
    CLOUD["Gemini STT connector<br/>demo-only, operator sets CONVENE_STT=gemini<br/>(ADR-31)"] -.-> |"only if the operator turns it on,<br/>never an automatic fallback"| S1
```

```mermaid
pie showData
    title Peak model memory on the laptop (GB)
    "Reasoning · Llama 3.1 8B 4-bit" : 5.5
    "STT · Whisper turbo" : 1.8
    "Embedding · BGE-small (+torch)" : 0.6
```

---

## 10. After the meeting: the organisation's record

```mermaid
mindmap
  root((Convene))
    Live meeting
      Per-phone transcript
      Correct the speaker
      Live Q&A with sources
      Per-device health
    After the meeting
      Structured summary
      DOCX minutes
      Email chosen parts to chosen people
    Action items CON-16
      Edit owner, due date, status
      Global open-items list
      Notes from later meetings
    Policy repository CON-17
      PDF and DOCX upload
      Append-only versions
      Searchable through Q&A
    History CON-14
      Cross-meeting Q&A
      Rename and delete
      Summaries of several meetings
    Reports CON-18
      Date-range aggregation
      No AI call
      DOCX download
```

---

## 11. How it was built

### Task dependency graph

```mermaid
flowchart LR
    C01["01 Transport<br/>baseline"] --> C02["02 API<br/>contracts"] --> C03["03 Storage<br/>+ audit"] --> C04["04 FastAPI<br/>transport"]
    C04 --> C04B["04B Trusted<br/>HTTPS"]
    C04 --> C05["05 Local STT"] --> C06["06 Attribution<br/>+ correction"]
    C06 --> C07["07 Dashboard"]
    C06 --> C08["08 Chunking +<br/>embeddings"] --> C09["09 Live Q&A"]
    C06 --> C10["10 Summary"] --> C11["11 DOCX"]
    C07 & C09 & C11 --> C12["12 Offline<br/>end-to-end"]
    C12 --> C13["13 Shared<br/>device"]
    C12 --> C14["14 History Q&A"]
    C12 --> C15["15 Load<br/>validation"]
    C10 --> C16["16 Action<br/>items"] --> C18["18 Periodic<br/>reports"]
    C08 --> C17["17 Policy<br/>repository"]

    classDef done fill:#e0e7ff,stroke:#4f46e5,color:#1e1b4b
    classDef pending fill:#fef3c7,stroke:#d97706,color:#451a03
    class C01,C02,C03,C04,C05,C06,C07,C08,C09,C10,C11,C14,C16,C17,C18 done
    class C04B,C12,C13,C15 pending
```

Indigo nodes are implemented with automated tests. Amber nodes still need real hardware, people or both.

### Timeline (from the work logs)

```mermaid
gantt
    title Convene build, September 2026
    dateFormat YYYY-MM-DD
    axisFormat %d %b
    section Foundation
    Transport baseline + contracts + storage :done, 2026-09-21, 1d
    FastAPI transport                         :done, 2026-09-22, 1d
    Local STT + scheduler                     :done, 2026-09-22, 2d
    section Core demo
    Attribution + dashboard                   :done, 2026-09-23, 1d
    First real phone end-to-end               :milestone, 2026-09-25, 0d
    RAG, live Q&A, summary, DOCX              :done, 2026-09-26, 1d
    Demo scripts + UI redesign                :done, 2026-09-26, 1d
    section Organisation features
    History Q&A + action items                :done, 2026-09-26, 2d
    Policy repository                         :done, 2026-09-28, 3d
    Reports, email minutes, multi-summary     :done, 2026-09-30, 1d
    section Still to do
    Offline multi-phone rehearsals (CON-12)   :active, 2026-10-01, 3d
    Five-phone load validation (CON-15)       :2026-10-03, 2d
```

---

## 12. Key numbers and what is verified

| Metric | Value | Source |
|---|---|---|
| Automated tests (non-model suite) | **~820 passing** | `logs/email-report.md`, `logs/history.md` |
| STT model load / call | 1.3 s load · ~0.5–0.6 s per segment | `docs/models.md`, `logs/stt.md` |
| 5 phones, speech segments | **0 dropped**, event-loop lag p95 1 ms | `logs/stt.md` (synthetic) |
| Transcript line → searchable | ~2 s median, < 3 s target | `logs/rag.md` |
| STT latency with indexing on vs. off | No measurable slowdown | `logs/rag.md` |
| Q&A accuracy / fabrication | 36/36 correct · 0/36 fabricated | `logs/qa.md` |
| Summary structured-output failures | 0 of 18 runs | `logs/summary-export.md` |
| Network sockets during Q&A / summary | 0 (in-process network block) | `logs/qa.md`, `logs/summary-export.md` |
| Architecture decisions recorded | 34 ADRs | `docs/decisions.md` |

```mermaid
flowchart LR
    subgraph V["✅ Verified"]
        v1["Automated suite passes, offline"]
        v2["1 real phone → attributed live transcript"]
        v3["Q&A honesty + summary reliability<br/>with real models"]
        v4["No network use in-process"]
        v5["Real email send via Resend"]
    end
    subgraph N["⏳ Not run yet"]
        n1["2+ real phones, bleed test"]
        n2["Wi-Fi physically off"]
        n3["iOS Safari + Android trust, no CA install"]
        n4["3 full offline rehearsals"]
        n5["30–60 min soak with 5 phones"]
    end
```

---

## 13. Suggested slide outline (8–10 minutes)

1. **Hook:** "Who agreed to what in your last meeting?" Show the problem-statement lines.
2. **Idea:** every phone is a microphone and one laptop does the rest (Section 1).
3. **Live demo, part 1:** scan the QR code, speak, and watch the attributed transcript appear.
4. **Architecture** (Section 2): one process, WebRTC + WebSocket, SQLite.
5. **Why attribution is right** (Section 5): identity comes from the device, with confidence and correction.
6. **Live demo, part 2:** ask a question → cited answer. Ask about something never discussed → "no grounding".
7. **Honesty numbers** (Section 6 table).
8. **End the meeting:** summary → DOCX → email (Section 7).
9. **Beyond one meeting** (Section 10): action items, policies, reports, history.
10. **Engineering rigour:** pipeline drops chart (Section 4) + key numbers (Section 12).
11. **What's next:** shared-device voice enrollment, five-phone load validation, offline rehearsal.

### Demo backup plan
- Pre-recorded video of the full flow (planned in CON-12; record it before the demo).
- `scripts/start_demo.sh` for one-command startup; `scripts/verify_local_only.py` to show judges no public
  connections are open.
