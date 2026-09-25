# Data Model

SQLite (`data/convene.db`), plus `sqlite-vec` for chunk embeddings in the same file. This document is authoritative for field names/types — other docs reference these entities, never redefine their shape.

Items marked **(proposed)** are contract changes made by CON-02 that need project-lead confirmation (see `decisions.md` ADR-15 to ADR-18 and `logs/contracts.md`). Until confirmed, implement them as written here, because dependent tasks build on them; if the lead rejects one, this document and the ADR are amended together.

## Conventions

- **IDs:** UUID version 4, lowercase hyphenated text. The client generates `device_id`; the server generates every other ID. Non-UUID identifiers are limited to `AuditEvent.seq` and the client-visible `seq` cursor (integers) and to `Meeting.title`.
- **Timestamps:** every `TEXT (ISO 8601)` field is UTC with millisecond precision and a `Z` suffix, for example `2026-09-21T11:35:02.123Z`. Fixed precision keeps lexical order equal to chronological order, which the `(meeting_id, t_start)` index and the transcript ordering depend on. Time base for `t_start`/`t_end` is the server clock (defined by `stt-pipeline.md`).
- **Booleans:** `INTEGER` 0/1 in SQLite, JSON `true`/`false` in the API.

## Entities

### Meeting

| Field | Type | Notes |
|---|---|---|
| meeting_id | TEXT (UUID) | PK |
| title | TEXT, nullable | defaults to a timestamp-derived label if not set |
| status | TEXT | `created` \| `live` \| `ended` |
| created_at | TEXT (ISO 8601) | |
| started_at | TEXT (ISO 8601), nullable | set on first device connection |
| ended_at | TEXT (ISO 8601), nullable | |

### Device

One row per physical phone connection for a meeting.

| Field | Type | Notes |
|---|---|---|
| device_id | TEXT (UUID) | PK |
| meeting_id | TEXT (UUID) | FK → Meeting |
| joined_at | TEXT (ISO 8601) | |
| status | TEXT | `joining` \| `enrolling` \| `connected` \| `disconnected` \| `left` |
| is_shared | INTEGER (bool) | declared at join |
| declared_speaker_count | INTEGER | 1 if not shared |
| reconnect_count | INTEGER | default 0 |
| user_agent | TEXT, nullable | diagnostics |

### Participant

A person. For a non-shared device, exactly one Participant maps to one Device. For a shared device, multiple Participants map to one Device via enrollment.

| Field | Type | Notes |
|---|---|---|
| participant_id | TEXT (UUID) | PK |
| meeting_id | TEXT (UUID) | FK → Meeting |
| device_id | TEXT (UUID) | FK → Device |
| display_name | TEXT | entered at join or enrollment |
| enrollment_status | TEXT | `not_required` \| `pending` \| `enrolled` \| `failed` |

### SpeakerEnrollment

Only populated for Participants on a shared Device.

| Field | Type | Notes |
|---|---|---|
| enrollment_id | TEXT (UUID) | PK |
| participant_id | TEXT (UUID) | FK → Participant |
| embedding | BLOB | speaker embedding vector |
| sample_duration_s | REAL | |
| enrolled_at | TEXT (ISO 8601) | |
| quality_flag | TEXT | `ok` \| `low_confidence_sample` |

### Utterance

The core transcript record.

| Field | Type | Notes |
|---|---|---|
| utterance_id | TEXT (UUID) | PK |
| meeting_id | TEXT (UUID) | FK → Meeting |
| device_id | TEXT (UUID) | FK → Device |
| participant_id | TEXT (UUID), nullable | null only for an unresolved generic-label case |
| text | TEXT | |
| t_start | TEXT (ISO 8601) | |
| t_end | TEXT (ISO 8601) | |
| stt_confidence | REAL | 0–1. **An uncalibrated score, not a probability**: for the `mlx` runtime it is `exp(duration-weighted mean of the segments' avg_logprob) × (1 − mean no_speech_prob)`, clamped (`stt-pipeline.md`). It ranks how certain the model was of the tokens it chose; it does **not** tell speech from non-speech (measured: Whisper returns a stock phrase on silence at 0.6–0.8), which is the VAD gate's job |
| attribution_method | TEXT | `device` \| `enrolled` \| `generic_unresolved` \| `manual_correction` |
| attribution_confidence | REAL | 0–1; fixed high for `device`, similarity-derived for `enrolled`, low for `generic_unresolved`, 1.0 for `manual_correction` |
| corrected | INTEGER (bool) | default 0 |
| original_participant_id | TEXT (UUID), nullable | set only if `corrected = 1`, preserves what attribution originally said |
| created_at | TEXT (ISO 8601) | |

Index: `(meeting_id, t_start)`, `(meeting_id, participant_id)`.

### ConnectionEvent

| Field | Type | Notes |
|---|---|---|
| event_id | TEXT (UUID) | PK |
| device_id | TEXT (UUID) | FK → Device |
| meeting_id | TEXT (UUID) | FK → Meeting |
| event_type | TEXT | `connected` \| `disconnected` \| `reconnected` \| `audio_resumed` |
| timestamp | TEXT (ISO 8601) | |

A `ConnectionEvent` is a query-friendly projection of one `AuditEvent` and is written in the same transaction as it (`connected` → `device_connected`, `disconnected` → `device_disconnected`, `reconnected` → `device_reconnected`, `audio_resumed` → `device_audio_resumed`). The `ConnectionEvent.event_id` equals the `event_id` of the audit event it projects, which links the two rows. The audit event is the record; if the two ever disagree, the audit event wins.

### TranscriptChunk

Chunked, embedded units for RAG. One chunk covers a contiguous run of Utterances respecting speaker turns.

| Field | Type | Notes |
|---|---|---|
| chunk_id | TEXT (UUID) | PK |
| meeting_id | TEXT (UUID) | FK → Meeting |
| utterance_id_start | TEXT (UUID) | FK → Utterance |
| utterance_id_end | TEXT (UUID) | FK → Utterance |
| text | TEXT | concatenated utterance text with speaker/time metadata inline |
| chunk_index | INTEGER | unique within the meeting, in creation order (see below); stable across rebuilds |
| status | TEXT | `pending` \| `ready` \| `failed`; a retryable index state, not a retrieval result |
| error_message | TEXT, nullable | short diagnostic when `status = failed` |
| is_closed | BOOLEAN | false for the one mutable meeting tail; true once the chunk has reached a safe boundary or the meeting ended |
| created_at | TEXT (ISO 8601) | |

`TranscriptChunk` rows are the durable text and citation records. Their vectors live in the
`TranscriptChunkVector` `sqlite-vec` virtual table, keyed by the same `chunk_id` and partitioned by
`meeting_id`; vectors are deliberately not duplicated in the row table. `TranscriptIndexMeta` holds the
single active embedding model identifier and vector dimension. Startup rejects a configured model or dimension
that differs from this record rather than mixing vector spaces. A chunk is `pending` until its vector write
succeeds and `failed` after an embedding/vector error; its source utterances are retained for retry.

The utterance ranges of a meeting's chunks never overlap and together cover every utterance once indexing has
caught up. The one open chunk is the meeting tail: it is re-chunked and re-embedded (after the settle window) as
speech arrives, so it is searchable while still open, and it closes when it reaches the target size at a speaker
change, the hard cap, or when the meeting ends. A closed chunk keeps its `chunk_id` and `chunk_index` when a
correction or a late result changes its text; when a rebuild no longer fits the hard cap, the overflow becomes a
new chunk with the next `chunk_index`, so `chunk_index` is creation order and time order comes from the utterance
range. An utterance too long for one chunk is split at sentence boundaries into several chunks that share its
utterance range and contain nothing else.

### QAQuery

| Field | Type | Notes |
|---|---|---|
| query_id | TEXT (UUID) | PK |
| meeting_id | TEXT (UUID), nullable | null when `mode = history` spans multiple meetings |
| mode | TEXT | `live` \| `history` |
| question | TEXT | |
| answer | TEXT, nullable | null if retrieval found nothing/failed |
| cited_chunk_ids | TEXT (JSON array) | |
| status | TEXT | `answered` \| `no_grounding` \| `failed` |
| created_at | TEXT (ISO 8601) | the moment the question was asked (`asked_at` for the as-of rule) |

`answer` is non-null exactly when `status = answered`. `cited_chunk_ids` holds the chunks given to the model as
evidence (empty unless answered). The reason for `no_grounding`/`failed` is in the `qa_query` audit event
(`rag-and-qa.md`, ADR-22), not on this row.

### Summary

| Field | Type | Notes |
|---|---|---|
| summary_id | TEXT (UUID) | PK |
| meeting_id | TEXT (UUID) | FK → Meeting |
| status | TEXT | **(proposed)** `pending` \| `ready` \| `failed`. A row is created as `pending` when an attempt starts. The current summary of a meeting is its most recent `ready` row; a `failed` attempt never replaces it |
| summary_text | TEXT, nullable | **(proposed)** null unless `status = ready`; malformed model output is never stored here |
| error_message | TEXT, nullable | **(proposed)** short diagnostic when `status = failed` |
| model_identifier | TEXT | which local (or cloud) model produced it |
| generated_at | TEXT (ISO 8601) | completion time of the attempt (success or failure) |

### ActionItem

| Field | Type | Notes |
|---|---|---|
| action_item_id | TEXT (UUID) | PK |
| summary_id | TEXT (UUID) | FK → Summary |
| meeting_id | TEXT (UUID) | FK → Meeting |
| text | TEXT | |
| owner_participant_id | TEXT (UUID), nullable | set if the LLM/user identified an owner |
| status | TEXT | `open` \| `done` |

### Export

| Field | Type | Notes |
|---|---|---|
| export_id | TEXT (UUID) | PK |
| meeting_id | TEXT (UUID) | FK → Meeting |
| type | TEXT | `docx` |
| status | TEXT | **(proposed)** `pending` \| `ready` \| `failed`. A `failed` attempt never replaces the current `ready` file |
| storage_path | TEXT, nullable | relative path under `data/exports/`; null unless `status = ready` |
| error_message | TEXT, nullable | **(proposed)** short diagnostic when `status = failed` |
| created_at | TEXT (ISO 8601) | |

### ModelExecution

One row per model invocation, for observability and post-hoc debugging of latency/quality issues. Written for every STT invocation, including ones that failed (the matching `model_error` audit event says which); windows the VAD gate or the overload policy dropped never reached the model and have no row.

| Field | Type | Notes |
|---|---|---|
| model_execution_id | TEXT (UUID) | PK |
| resource_type | TEXT | `stt` \| `embedding` \| `speaker_embedding` \| `reasoning` |
| model_identifier | TEXT | e.g. `mlx-community/whisper-large-v3-turbo` |
| runtime | TEXT | `mlx` \| `sentence_transformers` \| `groq` \| `gemini` |
| duration_ms | INTEGER | |
| related_id | TEXT, nullable | utterance_id, query_id, or summary_id depending on caller. For `stt` it is a window reference, `<device_id>/<window_id>`, because a transcribed window exists before, and often without, an `Utterance`. For `embedding` there is one row per embedding batch and it is the batch's first `chunk_id` |
| created_at | TEXT (ISO 8601) | |

### AuditEvent

The single source of truth (ADR-13) — see `architecture.md` lifecycle sections for which events fire where.

| Field | Type | Notes |
|---|---|---|
| event_id | TEXT (UUID) | PK |
| seq | INTEGER | **(proposed)** unique, strictly increasing across the whole database, assigned by the single `emit` write path inside the same transaction as the row (`max(seq) + 1`; there is one writer). It is the ordering key for dashboard resynchronization (`api.md`). It survives restarts because it is stored |
| meeting_id | TEXT (UUID), nullable | null only for the types that can occur outside a single meeting: `server_started`, `model_load`, `model_error`, `signaling_error` (a join for an unknown meeting), and `qa_query` (a history query across several meetings). Every other type requires it, and `emit` enforces that |
| event_type | TEXT | one of the catalog below; a value outside the catalog is rejected by the `emit` path |
| component | TEXT | emitting component: `api`, `transport`, `registry`, `stt`, `attribution`, `speaker`, `rag`, `summary`, `export`, `models` |
| timestamp | TEXT (ISO 8601) | |
| payload | TEXT (JSON) | object with the keys listed for that `event_type` |

Index: `(meeting_id, timestamp)`, `(event_type, timestamp)`, unique `(seq)`.

#### Audit event catalog

The complete set. Payloads reference records by ID and never contain transcript text, question text, or audio, so the audit stream can be inspected without exposing the meeting content. A nullable key is present with value `null`, never omitted. Adding an event type means adding a row here first.

| event_type | component | payload keys |
|---|---|---|
| `server_started` | `api` | `reconciled_devices`, `reconciled_meetings` |
| `meeting_created` | `api` | `title` |
| `meeting_started` | `transport` | `first_device_id` |
| `meeting_ended` | `api` | `utterance_count`, `device_count` |
| `hook_failed` | `api` | `hook`, `error` |
| `device_registered` | `registry` | `device_id`, `is_shared`, `declared_speaker_count`, `user_agent` |
| `device_connected` | `transport` | `device_id`, `reconnect_count` |
| `device_reconnected` | `transport` | `device_id`, `reconnect_count`, `via`, `remote_addr`, `user_agent` |
| `device_disconnected` | `transport` | `device_id`, `reason` |
| `device_audio_resumed` | `transport` | `device_id`, `gap_ms` |
| `device_left` | `transport` | `device_id`, `reason` |
| `signaling_error` | `transport` | `device_id`, `code`, `remote_addr` |
| `stt_window_dropped` | `stt` | `device_id`, `window_id`, `reason` |
| `model_load` | `models` | `resource_type`, `model_identifier`, `runtime`, `duration_ms` |
| `model_error` | `models` | `resource_type`, `model_identifier`, `device_id`, `window_id`, `related_id`, `error` |
| `utterance_created` | `attribution` | `utterance_id`, `device_id`, `participant_id`, `attribution_method`, `attribution_confidence`, `stt_confidence` |
| `utterance_corrected` | `attribution` | `utterance_id`, `from`, `to`, `changed`, `created_participant_id` |
| `enrollment_completed` | `speaker` | `device_id`, `participant_id`, `enrollment_id`, `quality_flag`, `sample_duration_s` |
| `enrollment_failed` | `speaker` | `device_id`, `participant_id`, `reason` |
| `index_failed` | `rag` | `utterance_id_start`, `utterance_id_end`, `error` |
| `qa_query` | `rag` | `query_id`, `mode`, `status`, `meeting_ids`, `chunk_count`, `duration_ms`, `error_code`, `reason`, `best_similarity` |
| `summary_started` | `summary` | `summary_id`, `trigger` |
| `summary_generated` | `summary` | `summary_id`, `input_as_of_seq`, `model_identifier`, `action_item_count` |
| `summary_failed` | `summary` | `summary_id`, `error_code`, `attempts` |
| `export_created` | `export` | `export_id`, `summary_id`, `input_as_of_seq`, `type` |
| `export_failed` | `export` | `export_id`, `error_code` |

Payload value sets:

- `hook_failed` records an isolated asynchronous hook or live-path callback (for example attribution post-write,
  correction re-indexing, or the summarization trigger on meeting end) that raised; the failure never rolls back
  the durable action that fired the hook. `hook` is the hook's name and `error` a short message.
- `device_reconnected.via`: `ice_restart` \| `new_peer`. `device_disconnected.reason`: `peer_disconnected` \| `peer_failed` \| `peer_closed` \| `server_restart`. `device_left.reason`: `client_leave` \| `meeting_ended`. `stt_window_dropped.reason`: `overload`. `summary_started.trigger`: `meeting_end` \| `manual`. `qa_query.error_code`: null unless `status = failed`, then `retrieval_failed` \| `generation_failed`. `qa_query.reason`: null for `answered`; for `no_grounding` `nothing_transcribed_yet` \| `not_indexed_yet` \| `no_relevant_evidence` \| `model_declined`; for `failed` `index_unavailable` \| `retrieval_failed` \| `answer_failed` \| `answer_timeout` (`rag-and-qa.md`). `qa_query.best_similarity`: the highest cosine similarity of any eligible chunk, or null when retrieval did not run.
- `utterance_corrected.from` is `{participant_id, attribution_method, attribution_confidence, corrected}` as the utterance stood immediately before this correction; `to` is `{participant_id, attribution_method, attribution_confidence}` after it. `changed` is false for a confirmation (same participant); `created_participant_id` is set when the correction created a new `Participant`. The full original attribution of a first correction is therefore always recoverable from this payload, even though the `Utterance` row keeps only `original_participant_id`.
- Device attribution uses configured confidence `0.95`, unresolved shared-device attribution uses `0.2`, and
  manual correction uses `1.0`. The initial server-owned low-confidence threshold is `0.8`; these tunable
  values live under `[attribution]`, not in application logic. One successful STT speech segment creates one
  `Utterance`.
- `summary_generated.input_as_of_seq` and `export_created.input_as_of_seq` are the `seq` high-water mark of the transcript the artifact was built from. They are how staleness is derived (see below), so no extra column is needed.

<!-- example: audit -->
```json
{
  "event_id": "5b1c0f3e-6a54-4c9e-9f0a-2d7d3b8f6a11",
  "seq": 412,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "event_type": "utterance_corrected",
  "component": "attribution",
  "timestamp": "2026-09-21T11:41:07.552Z",
  "payload": {
    "utterance_id": "9c2a7e10-3b4d-4f6a-8e15-7a0d5c1b2e34",
    "from": {
      "participant_id": null,
      "attribution_method": "generic_unresolved",
      "attribution_confidence": 0.2,
      "corrected": false
    },
    "to": {
      "participant_id": "3e8b1d47-52a9-4c60-b7f2-0a9c6d4e8b15",
      "attribution_method": "manual_correction",
      "attribution_confidence": 1.0
    },
    "changed": true,
    "created_participant_id": null
  }
}
```

## Derived state and live gauges

Which values are derived from the audit stream and which are live gauges (resolving the tension between ADR-13 and values that change every second):

- **Derived from `AuditEvent` / `ConnectionEvent` (durable, replayable):** device `status`, `reconnect_count`, every connection change, meeting `status`, utterance creation and correction, Q&A results, summary and export outcomes, and **staleness**. A summary is *stale* when the latest `utterance_corrected` or `utterance_created` audit event for the meeting has `seq` greater than the current summary's `summary_generated.input_as_of_seq`. An export is stale when its `export_created.input_as_of_seq` is below the same high-water mark or below a newer current summary's. No stored flag exists; nothing can disagree with the stream.
- **Live gauges (ephemeral, never persisted, never used to answer "what happened"):** `last_audio_age_ms`, `audio_duration_s`, `stt_backlog`, `stt_dropped_windows`. `stt_backlog` is the number of the device's windows queued for or being transcribed; `stt_dropped_windows` is how many the overload policy has dropped since the server started (each drop is also an audit event). They are computed in memory, served in `GET /api/meetings/{meeting_id}` and the `device_gauges` dashboard event (`api.md`), and reset by a server restart. A gauge never contradicts the audit stream because it describes the present instant, not history; the audit-visible consequences (a drop, a reconnect, a resumed stream) are separate events.

## Retention

No automatic deletion for the hackathon build — data volumes at this scale don't need it. Retention policy is an explicit deferred item (see `requirements.md` non-goals), not an oversight.
