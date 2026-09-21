# API Surface

Single FastAPI process (ADR-01). REST for request/response operations, WebSocket for signaling and live push feeds. This document is authoritative for endpoint shape — other docs describe *when* these are called, not their exact contracts.

## WebSocket: `/ws/signal/{meeting_id}`

Used by the phone client only. See `transport.md` for message shapes (`join`, `offer`, `answer`, `ice-candidate`, `reconnect`).

## WebSocket: `/ws/dashboard/{meeting_id}`

Used by the live dashboard only. Server pushes, client does not send data messages (read-only feed).

Server → client push events:

```json
{ "type": "utterance", "utterance": { "...": "Utterance row, data-model.md" } }
{ "type": "connection_event", "event": { "...": "ConnectionEvent row" } }
{ "type": "qa_answer", "query": { "...": "QAQuery row" } }
{ "type": "summary_ready", "summary_id": "..." }
```

## REST: Meetings

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /api/meetings` | Create a meeting | Returns `meeting_id`, join URL/QR payload |
| `GET /api/meetings` | List meetings (history view) | Supports basic filter by date/title |
| `GET /api/meetings/{meeting_id}` | Meeting detail: status, participants, devices | |
| `POST /api/meetings/{meeting_id}/end` | Explicit meeting-end action | Triggers summarization (`summarization.md`) |
| `GET /api/meetings/{meeting_id}/transcript` | Full ordered, attributed transcript | Includes confidence/method fields |

## REST: Devices & enrollment

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /api/meetings/{meeting_id}/devices` | Register a device at join | Body includes `is_shared`, `declared_speaker_count` |
| `POST /api/meetings/{meeting_id}/devices/{device_id}/enroll` | Submit one speaker's enrollment sample | Called once per declared speaker on a shared device — see `speaker-attribution.md` |

## REST: Attribution correction

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /api/meetings/{meeting_id}/utterances/{utterance_id}/correct` | Reassign an utterance to a different/named participant | Sets `attribution_method = "manual_correction"` — see `speaker-attribution.md` |

## REST: Q&A

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /api/meetings/{meeting_id}/qa` | Ask a question, `mode: "live"` | Scoped to this meeting only, up to the current point in time |
| `POST /api/qa` | Ask a question, `mode: "history"` | Body specifies which meeting(s) to search, or all |

## REST: Summarization & export

| Method & path | Purpose | Notes |
|---|---|---|
| `POST /api/meetings/{meeting_id}/summarize` | Manually (re-)trigger summarization | Also called automatically on meeting end |
| `GET /api/meetings/{meeting_id}/export?format=docx` | Trigger/download export | Creates the `Export` row if it doesn't exist yet, otherwise serves the existing file |

## Error shape (all endpoints)

```json
{ "error": { "code": "string", "message": "string" } }
```

`code` values should map to the failure categories named throughout this doc set (e.g. `stt_failed`, `no_grounding`, `retrieval_failed`, `enrollment_low_quality`, `export_render_failed`) rather than generic strings — this makes the dashboard's failure-vs-empty-result distinction (`rag-and-qa.md`) implementable without string-matching hacks.

## What this document deliberately does not specify

Authentication headers, rate limiting, or versioning — none apply here (ADR-10, single-meeting-ID access boundary, no public deployment target).
