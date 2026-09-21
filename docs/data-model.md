# Data Model

SQLite (`data/convene.db`), plus `sqlite-vec` for chunk embeddings in the same file. This document is authoritative for field names/types — other docs reference these entities, never redefine their shape.

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
| stt_confidence | REAL | model-reported, 0–1 |
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

### TranscriptChunk

Chunked, embedded units for RAG. One chunk covers a contiguous run of Utterances respecting speaker turns.

| Field | Type | Notes |
|---|---|---|
| chunk_id | TEXT (UUID) | PK |
| meeting_id | TEXT (UUID) | FK → Meeting |
| utterance_id_start | TEXT (UUID) | FK → Utterance |
| utterance_id_end | TEXT (UUID) | FK → Utterance |
| text | TEXT | concatenated utterance text with speaker/time metadata inline |
| chunk_index | INTEGER | order within the meeting |
| embedding | vector (via `sqlite-vec`) | see `rag-and-qa.md` for the embedding resource type |
| created_at | TEXT (ISO 8601) | |

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
| created_at | TEXT (ISO 8601) | |

### Summary

| Field | Type | Notes |
|---|---|---|
| summary_id | TEXT (UUID) | PK |
| meeting_id | TEXT (UUID) | FK → Meeting |
| summary_text | TEXT | |
| model_identifier | TEXT | which local (or cloud) model produced it |
| generated_at | TEXT (ISO 8601) | |

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
| storage_path | TEXT | relative path under `data/exports/` |
| created_at | TEXT (ISO 8601) | |

### ModelExecution

One row per model invocation, for observability and post-hoc debugging of latency/quality issues.

| Field | Type | Notes |
|---|---|---|
| model_execution_id | TEXT (UUID) | PK |
| resource_type | TEXT | `stt` \| `embedding` \| `speaker_embedding` \| `reasoning` |
| model_identifier | TEXT | e.g. `mlx-community/whisper-large-v3-turbo` |
| runtime | TEXT | `mlx` \| `groq` \| `gemini` |
| duration_ms | INTEGER | |
| related_id | TEXT, nullable | utterance_id, query_id, or summary_id depending on caller |
| created_at | TEXT (ISO 8601) | |

### AuditEvent

The single source of truth (ADR-13) — see `architecture.md` lifecycle sections for which events fire where.

| Field | Type | Notes |
|---|---|---|
| event_id | TEXT (UUID) | PK |
| meeting_id | TEXT (UUID), nullable | |
| event_type | TEXT | `device_connected`, `device_disconnected`, `device_reconnected`, `utterance_created`, `utterance_corrected`, `enrollment_completed`, `enrollment_failed`, `qa_query`, `summary_generated`, `export_created`, `model_load`, `model_error` |
| component | TEXT | emitting component name |
| timestamp | TEXT (ISO 8601) | |
| payload | TEXT (JSON) | |

Index: `(meeting_id, timestamp)`, `(event_type, timestamp)`.

## Important rule

**AuditEvent is the single source of truth for "what happened and when."** The other tables exist for efficient structured querying (e.g. "give me this meeting's transcript in order" without parsing JSON payloads), but nothing about system behavior should ever be tracked in a way that could disagree with the AuditEvent stream. The live dashboard's connection-health and activity views are queries over AuditEvent, not a separately maintained state.

## Retention

No automatic deletion for the hackathon build — data volumes at this scale don't need it. Retention policy is an explicit deferred item (see `requirements.md` non-goals), not an oversight.
