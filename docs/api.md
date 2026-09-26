# API Surface

Single FastAPI process (ADR-01). REST for request/response operations, WebSocket for signaling and live push feeds. This document is authoritative for endpoint shape — other docs describe *when* these are called, not their exact contracts. Stored field names come from `data-model.md` and are never renamed here; where the API adds a computed field it is listed under [Derived fields](#derived-fields).

**Implementation status (through CON-09):** implemented and covered by automated loopback tests: `POST /api/meetings`, `GET /api/meetings/{meeting_id}`, `POST …/devices`, `POST …/end`, `GET …/transcript`, and `POST …/utterances/{utterance_id}/correct`; signaling and dashboard WebSockets; and the pages `/`, `/join/{meeting_id}`, `/dashboard/{meeting_id}`, and `/static/…`. The dashboard feed includes `meeting_status`, `device_status`, `connection_event`, `device_gauges`, `utterance`, and `utterance_updated`; the dashboard consumes server-computed labels and low-confidence state. Live Q&A (`POST …/qa`, the `qa_answer` push) is implemented by CON-09. Summarization (`POST …/summarize`, `GET …/summary`, the `summary_ready` and `summary_failed` pushes, summarization started by `end`, and the post-meeting page `/meetings/{meeting_id}`) is implemented by CON-10. `GET /api/meetings`, history Q&A (`POST /api/qa`), export, and enrollment routes remain unimplemented. None of this has been verified on real phones. A route section below describes the contract, not a claim that the route exists.

**Status of decisions:** contract choices that change a documented behavior or the schema are marked **(proposed)** and recorded as ADR-15 to ADR-18 in `decisions.md`, pending project-lead confirmation. Shapes marked **provisional** belong to should-have features and are finalized by CON-13 (enrollment) and CON-14 (history).

## Conventions

- **Transport and casing.** HTTPS on the server port, JSON bodies, `snake_case` field names everywhere (REST, WebSocket, data model). Clients ignore unknown response fields so the contract can grow additively.
- **IDs.** UUID v4, lowercase, hyphenated text, in URLs and bodies. The phone generates `device_id`; the server generates all others. The meeting UUID in the join URL is the sole access boundary (ADR-10). No short join code exists. (G1, proposed, ADR-15.) The only non-UUID identifiers are the integer `seq`/`as_of_seq`/`after_seq` cursors.
- **Timestamps.** ISO 8601 UTC with millisecond precision and a `Z` suffix, for example `2026-09-21T11:35:02.123Z`. Lexical order is chronological order.
- **Transcript ordering.** By `t_start` ascending, ties broken by `utterance_id` ascending, which is stable but not chronological. `t_start` is on the server clock (`stt-pipeline.md` defines the time base), so the relative order of lines from *different* devices is only as accurate as that clock and the windowing; treat close timestamps across devices as unordered. The dashboard also receives lines in arrival order and must sort by this rule.
- **`seq`.** A strictly increasing integer taken from `AuditEvent.seq` (`data-model.md`). Every durable dashboard event carries the `seq` of the audit event that caused it, and snapshot responses carry `as_of_seq`, the highest committed `seq` at the moment the snapshot was read. `seq` values are not contiguous per meeting (other meetings share the counter). Several pushes caused by one audit event may share a `seq`.
- **Content types.** Requests with a body use `application/json; charset=utf-8`; any other type is `415 unsupported_media_type`. JSON bodies are limited to 64 KiB (`413 payload_too_large`). Responses are `application/json; charset=utf-8` except the export download and the HTML pages. A `POST` with no body is treated as `{}`.
- **Validation.** Any malformed or out-of-range body, query parameter or path ID returns `400 invalid_request`. Limits: `title` at most 200 characters; `display_name` 1–80 characters after trimming; `question` 1–2000 characters after trimming; `declared_speaker_count` per the device section.
- **Idempotency.** Each endpoint states its own. Reads never change state, with the documented exception of `GET …/export`.
- **Q&A outcome versus error.** A Q&A request that is well-formed and reaches the retrieval layer always returns `200` with the persisted `QAQuery`, whether its `status` is `answered`, `no_grounding` or `failed`. The error envelope is reserved for request-level errors (unknown meeting, empty question, wrong meeting state). `no_grounding` and `failed` are results, not HTTP errors, so the dashboard can tell "the system does not know" from "the system is broken" (`rag-and-qa.md`). (G8, proposed, ADR-15.)

### Status codes

| Status | Use |
|---|---|
| 200 | Success; also an idempotent replay that changed nothing |
| 201 | A resource was created |
| 202 | Accepted; work continues in the background and its result arrives by push |
| 302 | The dashboard route of an ended meeting redirecting to the post-meeting view |
| 400 | Invalid request or unsupported format |
| 404 | Unknown meeting, device, participant, utterance or summary |
| 409 | Request is valid but the current state forbids it |
| 413 | Body over 64 KiB |
| 415 | Unsupported content type |
| 422 | Enrollment could not produce a usable sample (provisional) |
| 500 | Unexpected server error or export render failure |

### Error shape (all endpoints)

```json
{ "error": { "code": "meeting_not_found", "message": "no meeting with id 0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8" } }
```

`code` is a stable machine-readable string; `message` is for humans and may change. Codes map to the failure categories named across the docs rather than generic strings, so the dashboard's failure-versus-empty distinction is implementable without string matching.

| code | HTTP | meaning |
|---|---|---|
| `invalid_request` | 400 | malformed body, query parameter or ID; violated a limit |
| `unsupported_format` | 400 | export `format` other than `docx` |
| `meeting_not_found` | 404 | no such meeting |
| `device_not_found` | 404 | no such device in this meeting |
| `participant_not_found` | 404 | no such participant in this meeting |
| `utterance_not_found` | 404 | no such utterance in this meeting |
| `summary_not_found` | 404 | no summary attempt exists for this meeting |
| `meeting_ended` | 409 | the operation needs a meeting that has not ended |
| `meeting_not_ended` | 409 | history Q&A over a meeting that is still running (provisional) |
| `device_conflict` | 409 | the `device_id` is registered in another meeting, or registered here with a different `is_shared` |
| `ambiguous_display_name` | 409 | a correction by `display_name` matches more than one participant |
| `color_taken` | 409 | the requested participant colour is already used in this meeting (ADR-25) |
| `summary_in_progress` | 409 | a summary attempt is already running |
| `summary_not_ready` | 409 | export needs a `ready` summary and none exists |
| `transcript_empty` | 409 | nothing to summarize |
| `device_not_connected` | 409 | enrollment needs a connected device (provisional) |
| `device_not_shared` | 409 | enrollment applies only to shared devices (provisional) |
| `enrollment_in_progress` | 409 | an enrollment capture is already running on this device (provisional) |
| `payload_too_large` | 413 | body over 64 KiB |
| `unsupported_media_type` | 415 | not `application/json` |
| `enrollment_low_quality` | 422 | no usable sample could be embedded (provisional) |
| `export_render_failed` | 500 | rendering the DOCX failed; summary and transcript are unaffected |
| `internal_error` | 500 | unexpected server error |

Failure codes that appear inside successful responses, pushes, or audit payloads rather than as HTTP errors: `retrieval_failed` and `generation_failed` (a `QAQuery` with `status = failed`), `summary_generation_failed`, `summary_invalid_output`, `transcript_too_long` and `transcript_empty` (the `summary_failed.error_code` of a failed `Summary`, `summarization.md`), `stt_failed`, and `enrollment_failed`. `no_grounding` is a `QAQuery.status` value, not an error code.

### Derived fields

API views may add computed fields to a stored entity. They are computed on the server, are not stored, and are the only fields an example may contain beyond the entity's table in `data-model.md`. The consistency test (`tests/test_api_contract_docs.py`) reads this table.

| Entity | Derived field | Meaning |
|---|---|---|
| Meeting | `participant_count` | number of participants in the meeting (list view) |
| Device | `participants` | the device's `Participant` rows |
| Device | `gauges` | live gauges (`last_audio_age_ms`, `audio_duration_s`, `stt_backlog`, `stt_dropped_windows`), all `null` for a device that has not streamed since the server started |
| Utterance | `seq` | `seq` of the utterance's `utterance_created` audit event |
| Utterance | `speaker_label` | display name of the current participant; if `participant_id` is null, `Speaker on Phone <n>` where `<n>` is the device's 1-based rank by `joined_at` in the meeting (ties by `device_id`) |
| Utterance | `low_confidence` | true when `attribution_confidence` is below the server's configured threshold or `attribution_method` is `generic_unresolved`. The threshold is server configuration; **clients never compare confidence numbers** (G20) |
| Summary | `stale` | the transcript changed after the summary was built (`data-model.md`, "Derived state") |
| Export | `stale` | the transcript or current summary changed after the file was rendered |
| ActionItem | `owner_display_name` | display name of `owner_participant_id`, or null |
| QAQuery | `error` | null unless `status = failed`, then `{ "code": "retrieval_failed" \| "generation_failed", "message": "..." }` |

## Route index

| Method | Path | Tier |
|---|---|---|
| POST | `/api/meetings` | MVP |
| GET | `/api/meetings` | provisional (CON-14) |
| GET | `/api/meetings/{meeting_id}` | MVP |
| POST | `/api/meetings/{meeting_id}/devices` | MVP |
| GET | `/api/meetings/{meeting_id}/colors` | MVP (ADR-25) |
| POST | `/api/meetings/{meeting_id}/devices/{device_id}/enroll` | provisional (CON-13) |
| POST | `/api/meetings/{meeting_id}/end` | MVP |
| GET | `/api/meetings/{meeting_id}/transcript` | MVP |
| POST | `/api/meetings/{meeting_id}/utterances/{utterance_id}/correct` | MVP |
| POST | `/api/meetings/{meeting_id}/qa` | MVP |
| POST | `/api/qa` | provisional (CON-14) |
| POST | `/api/meetings/{meeting_id}/summarize` | MVP |
| GET | `/api/meetings/{meeting_id}/summary` | MVP |
| GET | `/api/meetings/{meeting_id}/export` | MVP |
| WS | `/ws/signal/{meeting_id}` | MVP |
| WS | `/ws/dashboard/{meeting_id}` | MVP |

`GET …/summary` is a route added by CON-02 (gap X1): the post-meeting view needs to read the summary and action items, and no earlier route returned them. The DT-17 prototype's `GET /metrics` is not part of this contract; CON-04 may keep it as a debug route but nothing may depend on it.

## Served pages

These return HTML for a browser, not JSON. Assets are served from the same process at `/static/…`; no page loads anything from a CDN or another origin (local-first). An unknown meeting ID renders a plain "meeting not found" page with status 404.

| Method | Path | Surface (`frontend.md`) | Status |
|---|---|---|---|
| GET | `/` | Meeting history view, with a "New meeting" button that calls `POST /api/meetings` | 200 |
| GET | `/join/{meeting_id}` | Join page (phone) | 200, 404 |
| GET | `/dashboard/{meeting_id}` | Live dashboard, including the join QR while the meeting is `created` or `live` | 200, 302 to `/meetings/{meeting_id}` if the meeting has ended, 404 |
| GET | `/meetings/{meeting_id}` | Post-meeting view: summary, action items, export, and Q&A in history mode | 200, 404 |
| GET | `/static/{path}` | Scripts, styles and images | 200, 404 |

The prototype's `GET /app.js` is replaced by `/static/`. Serving these pages is the frontend tasks' work; the routes exist so no page needs a guessed URL.

## REST: Meetings

### POST /api/meetings

Create a meeting and get its join link.

**Request** — body optional.

```json
{ "title": "Sprint planning" }
```

`title` is optional; null, empty or omitted means a default label derived from the creation time in the server's local timezone, for example `Meeting 2026-09-21 17:00`.

**Response** — `201`. `join_url` is `https://<host>:<port>/join/<meeting_id>`, where `<host>` is the configured `--public-host`/`network.public_host` in trusted-host mode, otherwise the detected or `--advertise-ip` private IPv4 address. `qr_svg` is an inline SVG document encoding `join_url` and nothing else. If neither a public host nor LAN address exists, the meeting is still created and `join_url` and `qr_svg` are `null` with `"no_lan_address"` in `warnings`.

```json
{
  "meeting": {
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "title": "Sprint planning",
    "status": "created",
    "created_at": "2026-09-21T11:30:00.000Z",
    "started_at": null,
    "ended_at": null
  },
  "join_url": "https://192.168.50.10:8443/join/0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "qr_svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 37 37\">…</svg>",
  "warnings": []
}
```

**Status codes**

| Status | Code | When |
|---|---|---|
| 201 | — | created |
| 400 | `invalid_request` | `title` too long or not a string |
| 413 | `payload_too_large` | body over 64 KiB |
| 415 | `unsupported_media_type` | not JSON |

**Side effects** — inserts a `Meeting` (`created`); audit `meeting_created`. Not idempotent: every call creates a new meeting. The meeting becomes `live` on the first device that reaches `connected` (audit `meeting_started`, push `meeting_status`).

### GET /api/meetings

**Provisional (CON-14).** List meetings for the history view, newest first.

**Request** — query parameters, all optional: `q` (case-insensitive substring of `title`), `status` (`created`, `live` or `ended`), `from` and `to` (inclusive `created_at` bounds, ISO 8601 UTC), `limit` (default 50, maximum 200) and `offset` (default 0).

**Response** — `200`.

```json
{
  "meetings": [
    {
      "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
      "title": "Sprint planning",
      "status": "ended",
      "created_at": "2026-09-21T11:30:00.000Z",
      "started_at": "2026-09-21T11:31:12.000Z",
      "ended_at": "2026-09-21T12:02:40.000Z",
      "participant_count": 3
    }
  ],
  "total": 1
}
```

**Status codes**

| Status | Code | When |
|---|---|---|
| 200 | — | success, possibly an empty list |
| 400 | `invalid_request` | bad `status`, date, `limit` or `offset` |

**Side effects** — none. Read-only.

### GET /api/meetings/{meeting_id}

Meeting detail and the dashboard's snapshot of devices and live health.

**Request** — no body.

**Response** — `200`. `devices` lists every registered device, each with its participants and live gauges. `latest_summary` and `latest_export` are the most recent attempt of each, or null. `summary_pending` is true while a summary attempt is running. `as_of_seq` is the snapshot cursor for [resynchronization](#resynchronization).

```json
{
  "meeting": {
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "title": "Sprint planning",
    "status": "live",
    "created_at": "2026-09-21T11:30:00.000Z",
    "started_at": "2026-09-21T11:31:12.000Z",
    "ended_at": null
  },
  "devices": [
    {
      "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18",
      "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
      "joined_at": "2026-09-21T11:31:05.000Z",
      "status": "connected",
      "is_shared": false,
      "declared_speaker_count": 1,
      "reconnect_count": 1,
      "user_agent": "Mozilla/5.0 (Linux; Android 14) Chrome/128.0",
      "participants": [
        {
          "participant_id": "3e8b1d47-52a9-4c60-b7f2-0a9c6d4e8b15",
          "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
          "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18",
          "display_name": "Priya",
          "enrollment_status": "not_required",
          "color": "sky"
        }
      ],
      "gauges": {
        "last_audio_age_ms": 40,
        "audio_duration_s": 312.4,
        "stt_backlog": 0,
        "stt_dropped_windows": 0
      }
    }
  ],
  "join_url": "https://192.168.50.10:8443/join/0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "qr_svg": "<svg xmlns=\"http://www.w3.org/2000/svg\" viewBox=\"0 0 37 37\">…</svg>",
  "latest_summary": null,
  "latest_export": null,
  "summary_pending": false,
  "as_of_seq": 431
}
```

`join_url` and `qr_svg` are the values `POST /api/meetings` returned, so a reloaded dashboard can show the join QR again. They are `null` once the meeting has ended or when neither a configured public host nor LAN address is available.

**Status codes**

| Status | Code | When |
|---|---|---|
| 200 | — | success |
| 404 | `meeting_not_found` | unknown meeting |

**Side effects** — none. Gauges are read from memory and are not audit-derived (`data-model.md`).

### POST /api/meetings/{meeting_id}/devices

Register a device at join. **REST registers; the signaling WebSocket only attaches** (G3, `transport.md`): a WebSocket `join` for a device that was not registered here is rejected.

**Request**

```json
{
  "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18",
  "display_name": "Priya",
  "is_shared": false,
  "declared_speaker_count": 1,
  "color": "teal"
}
```

`color` is optional: one of the 12 palette keys `lime`, `green`, `teal`, `cyan`, `sky`, `azure`, `violet`, `plum`, `magenta`, `pink`, `slate`, `charcoal` (ADR-25), only for a non-shared device. Omitted or null, the server assigns a random colour not yet used in the meeting (once all 12 are used, one of the least used). The Convene brand colour is never a participant colour.

`device_id` is a UUID the phone generated and persisted (`transport.md`). For a non-shared device (`is_shared` false or omitted) `display_name` is required and `declared_speaker_count` must be 1 or omitted. For a shared device, `declared_speaker_count` is 2 or 3 and `display_name` is omitted; its participants are created one by one at enrollment (provisional, CON-13).

**Response** — `201` for a new registration, `200` for an idempotent replay of the same `device_id` in the same meeting, which returns the stored rows unchanged (a different `display_name` or `color` in the replay is ignored, and the response shows the values in force, so a rejoining phone keeps its colour). A non-shared device gets exactly one `Participant` with `enrollment_status = not_required`; the server issues its `participant_id`.

```json
{
  "device": {
    "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "joined_at": "2026-09-21T11:31:05.000Z",
    "status": "joining",
    "is_shared": false,
    "declared_speaker_count": 1,
    "reconnect_count": 0,
    "user_agent": "Mozilla/5.0 (Linux; Android 14) Chrome/128.0",
    "participants": [
      {
        "participant_id": "3e8b1d47-52a9-4c60-b7f2-0a9c6d4e8b15",
        "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
        "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18",
        "display_name": "Priya",
        "enrollment_status": "not_required",
        "color": "teal"
      }
    ]
  }
}
```

`user_agent` comes from the request's `User-Agent` header. A shared device is created with `status = enrolling` and no participants yet.

**Status codes**

| Status | Code | When |
|---|---|---|
| 201 | — | new device registered |
| 200 | — | replay of an existing registration |
| 400 | `invalid_request` | missing or invalid `device_id`, `display_name`, speaker count or `color` |
| 409 | `color_taken` | the requested `color` is already used by a participant in this meeting |
| 404 | `meeting_not_found` | unknown meeting |
| 409 | `meeting_ended` | the meeting has ended |
| 409 | `device_conflict` | `device_id` is registered in a different meeting, or in this one with a different `is_shared` |
| 413 | `payload_too_large` | body over 64 KiB |
| 415 | `unsupported_media_type` | not JSON |

**Side effects** — on a new registration: inserts `Device` and, for a non-shared device, its `Participant` in one transaction; audit `device_registered`; push `device_status`. A replay writes nothing and pushes nothing. Registration does not start the meeting.

### GET /api/meetings/{meeting_id}/colors

The participant palette for the join page's colour picker (ADR-25), with the keys this meeting already uses.

**Request** — no body or parameters.

**Response** — `200`. All 12 keys, in palette order (shortened below). `taken` is a snapshot: two phones can still
pick the same colour at once, and the second registration then gets `409 color_taken`.

```json
{
  "colors": [
    { "color": "lime", "taken": false },
    { "color": "teal", "taken": true }
  ]
}
```

**Status codes**

| Status | Code | When |
|---|---|---|
| 200 | — | palette returned |
| 404 | `meeting_not_found` | unknown meeting |

**Side effects** — none; read-only, no audit event.

### POST /api/meetings/{meeting_id}/end

Explicit meeting-end action; triggers summarization (`summarization.md`).

**Request** — empty body or `{}`.

**Response** — `202` the first time, `200` if the meeting had already ended (nothing further happens). `summary_pending` is true when a summary attempt was started (and, on the `200` replay, while it is still running). An attempt starts when the meeting has at least one utterance or still has speech queued for transcription; an empty transcript starts none. If a manually triggered attempt is already running when the meeting ends, no second attempt starts; lines it missed make it stale.

```json
{
  "meeting": {
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "title": "Sprint planning",
    "status": "ended",
    "created_at": "2026-09-21T11:30:00.000Z",
    "started_at": "2026-09-21T11:31:12.000Z",
    "ended_at": "2026-09-21T12:02:40.000Z"
  },
  "summary_pending": true
}
```

**Status codes**

| Status | Code | When |
|---|---|---|
| 202 | — | ended now; background work started |
| 200 | — | already ended; no change |
| 404 | `meeting_not_found` | unknown meeting |

**Side effects** — `Meeting.status = ended`, `ended_at` set; audit `meeting_ended`; push `meeting_status`. Every attached phone gets `meeting_ended` and its peer is closed; each device becomes `left` (audit `device_left`, reason `meeting_ended`, push `device_status`). The server then lets in-flight STT windows finish and flushes pending chunks, so late lines still reach the transcript and appear as `utterance` pushes, and only then starts summarization (`summary_started`, later `summary_ready` or `summary_failed`). After a successful summary the DOCX is rendered automatically (`export.md`), pushing `export_ready` or `export_failed`. Idempotent.

### GET /api/meetings/{meeting_id}/transcript

The full, ordered, attributed transcript with confidence and method fields.

**Request** — query parameter `after_seq` (optional integer ≥ 0). Without it the response is the complete transcript. With it, only utterances whose `seq` is greater are returned, for incremental fetches. Corrections made to earlier utterances are not included in an incremental fetch; they arrive as `utterance_updated`, or in a full fetch. There is no pagination; a hackathon-scale meeting is a few thousand lines.

**Response** — `200`, ordered per the [ordering rule](#conventions). `speaker_label` and `low_confidence` are computed server-side.

```json
{
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "utterances": [
    {
      "utterance_id": "9c2a7e10-3b4d-4f6a-8e15-7a0d5c1b2e34",
      "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
      "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18",
      "participant_id": "3e8b1d47-52a9-4c60-b7f2-0a9c6d4e8b15",
      "text": "We should ship the beta on Friday.",
      "t_start": "2026-09-21T11:34:12.400Z",
      "t_end": "2026-09-21T11:34:14.100Z",
      "stt_confidence": 0.93,
      "attribution_method": "device",
      "attribution_confidence": 0.95,
      "corrected": false,
      "original_participant_id": null,
      "created_at": "2026-09-21T11:34:15.020Z",
      "seq": 401,
      "speaker_label": "Priya",
      "low_confidence": false
    },
    {
      "utterance_id": "1a6d3f82-9e07-4b5c-8a14-c2e9f0b7d635",
      "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
      "device_id": "c1a44d90-8f27-4b13-9d6e-52f0a7b3c8e4",
      "participant_id": null,
      "text": "I can take the release notes.",
      "t_start": "2026-09-21T11:34:16.900Z",
      "t_end": "2026-09-21T11:34:18.300Z",
      "stt_confidence": 0.88,
      "attribution_method": "generic_unresolved",
      "attribution_confidence": 0.2,
      "corrected": false,
      "original_participant_id": null,
      "created_at": "2026-09-21T11:34:19.240Z",
      "seq": 405,
      "speaker_label": "Speaker on Phone 2",
      "low_confidence": true
    }
  ],
  "as_of_seq": 431
}
```

**Status codes**

| Status | Code | When |
|---|---|---|
| 200 | — | success, possibly an empty list |
| 400 | `invalid_request` | `after_seq` not a non-negative integer |
| 404 | `meeting_not_found` | unknown meeting |

**Side effects** — none. Windows that failed STT are not utterances and do not appear.

### POST /api/meetings/{meeting_id}/utterances/{utterance_id}/correct

Reassign an utterance to a different or newly named participant (ADR-04, `speaker-attribution.md`).

**Request** — exactly one of `participant_id` or `display_name`.

```json
{ "participant_id": "a8d20f6b-4c39-4e15-8b72-9e1c3d5a7f06" }
```

```json
{ "display_name": "Sam" }
```

- `participant_id` must belong to a participant of the same meeting on any device; reassigning a line to a participant on another phone is the normal fix for cross-device bleed.
- `display_name` names an unresolved speaker. If exactly one participant in the meeting already has that name (exact match after trimming), that participant is used; if none does, a new `Participant` is created on the *utterance's device* with `enrollment_status = not_required`; if more than one matches, the request fails with `ambiguous_display_name`.
- Correcting to the participant the line already has is allowed and means **confirm**: it records `manual_correction` with confidence 1.0, which removes the low-confidence marker, and the audit payload has `changed: false` (G10, proposed, ADR-17). A repeat of a correction that changes nothing further is a no-op.
- Corrections are allowed after the meeting ends, and make the summary and export stale.

**Response** — `200`. `changed` is true when the participant differs from the one before this request. `created_participant` is true when this request created one.

```json
{
  "utterance": {
    "utterance_id": "1a6d3f82-9e07-4b5c-8a14-c2e9f0b7d635",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "device_id": "c1a44d90-8f27-4b13-9d6e-52f0a7b3c8e4",
    "participant_id": "a8d20f6b-4c39-4e15-8b72-9e1c3d5a7f06",
    "text": "I can take the release notes.",
    "t_start": "2026-09-21T11:34:16.900Z",
    "t_end": "2026-09-21T11:34:18.300Z",
    "stt_confidence": 0.88,
    "attribution_method": "manual_correction",
    "attribution_confidence": 1.0,
    "corrected": true,
    "original_participant_id": null,
    "created_at": "2026-09-21T11:34:19.240Z",
    "seq": 405,
    "speaker_label": "Sam",
    "low_confidence": false
  },
  "participant": {
    "participant_id": "a8d20f6b-4c39-4e15-8b72-9e1c3d5a7f06",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "device_id": "c1a44d90-8f27-4b13-9d6e-52f0a7b3c8e4",
    "display_name": "Sam",
    "enrollment_status": "not_required",
    "color": "teal"
  },
  "created_participant": true,
  "changed": true
}
```

`original_participant_id` keeps the participant from the **first** correction only; here it is null because the line started as `generic_unresolved`, and `corrected: true` is what marks the difference from "never corrected". A second correction leaves it unchanged. The complete "from" state of every correction, including original method and confidence, is in the `utterance_corrected` audit payload.

**Status codes**

| Status | Code | When |
|---|---|---|
| 200 | — | corrected, confirmed, or unchanged replay |
| 400 | `invalid_request` | neither or both of `participant_id` and `display_name`, blank name, bad ID |
| 404 | `meeting_not_found` | unknown meeting |
| 404 | `utterance_not_found` | the utterance is not in this meeting |
| 404 | `participant_not_found` | `participant_id` is not in this meeting |
| 409 | `ambiguous_display_name` | more than one participant has that name |
| 413 | `payload_too_large` | body over 64 KiB |
| 415 | `unsupported_media_type` | not JSON |

**Side effects** — one transaction updates the utterance (`participant_id`, `attribution_method = manual_correction`, `attribution_confidence = 1.0`, `corrected = 1`, `original_participant_id` on the first correction only), inserts a `Participant` if one was created, and writes audit `utterance_corrected`. Pushes `utterance_updated` and, if a participant was created, `device_status`. Registered correction hooks then run without blocking the response: the chunk containing the utterance is re-indexed (`rag-and-qa.md`), and summary and export staleness change by derivation with no event. A no-op replay writes and pushes nothing.

### POST /api/meetings/{meeting_id}/qa

Ask a live question about this meeting so far (`mode: live`). This is the only way retrieval runs (ADR-11).

**Request**

```json
{ "question": "What did we decide about the beta launch date?", "mode": "live" }
```

`mode` is optional and must be `live` if present. Scope is this meeting only, as of the moment the question is asked (`rag-and-qa.md`).

**Response** — `200` for all three outcomes. `query` is the persisted `QAQuery` (its `cited_chunk_ids` shown as a JSON array); `citations` are resolved from stored chunk and utterance data, never from model text, so a citation always names a real stored line. `cited_chunk_ids` are exactly the chunks given to the model as evidence (deterministic, not chosen by the model). Each citation lists every speaker in its chunk (`speakers`, in order of speech) and every line (`utterance_ids`), so the dashboard can highlight them. Client rule: show the answer only for `answered`; for `no_grounding` show the honest "no grounding in this meeting" message; for `failed` show a system error. Never present them alike.

`reason` says why a query is `no_grounding` or `failed` (null for `answered`); it is also in the `qa_query` audit payload, and is not stored on the `QAQuery` row. `unindexed_utterances` counts lines of this meeting that were not yet searchable when the question was asked (the indexing lag, `rag-and-qa.md`).

| `status` | `reason` | `error.code` | Meaning |
|---|---|---|---|
| `answered` | null | — | grounded answer with citations |
| `no_grounding` | `nothing_transcribed_yet` | — | no line has been transcribed in this meeting |
| `no_grounding` | `not_indexed_yet` | — | lines exist but none is searchable yet, and the index is healthy |
| `no_grounding` | `no_relevant_evidence` | — | no chunk reached the relevance threshold; the model was not called |
| `no_grounding` | `model_declined` | — | the model found no answer in the retrieved excerpts |
| `failed` | `index_unavailable` | `retrieval_failed` | the index has failed chunks and nothing ready, has fallen behind by more than `[qa].index_stale_s`, or no embedding model is loaded |
| `failed` | `retrieval_failed` | `retrieval_failed` | embedding the question or the vector query failed |
| `failed` | `answer_failed` | `generation_failed` | the reasoning model failed, returned nothing, or is not loaded |
| `failed` | `answer_timeout` | `generation_failed` | no complete answer within `[qa].answer_timeout_s` |

Answered:

```json
{
  "query": {
    "query_id": "e4b7c2a1-90d3-4a58-b6f1-2c8d0a3e5b79",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "mode": "live",
    "question": "What did we decide about the beta launch date?",
    "answer": "Priya proposed shipping the beta on Friday and no one objected.",
    "cited_chunk_ids": ["2f9e6d13-a7c4-4b80-9e52-6d1b8a4c0f37"],
    "status": "answered",
    "created_at": "2026-09-21T11:40:03.310Z",
    "error": null
  },
  "citations": [
    {
      "chunk_id": "2f9e6d13-a7c4-4b80-9e52-6d1b8a4c0f37",
      "chunk_index": 12,
      "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
      "utterance_id_start": "9c2a7e10-3b4d-4f6a-8e15-7a0d5c1b2e34",
      "utterance_id_end": "1a6d3f82-9e07-4b5c-8a14-c2e9f0b7d635",
      "speakers": ["Priya", "Sam"],
      "t_start": "2026-09-21T11:34:12.400Z",
      "t_end": "2026-09-21T11:34:18.300Z",
      "utterance_ids": ["9c2a7e10-3b4d-4f6a-8e15-7a0d5c1b2e34", "1a6d3f82-9e07-4b5c-8a14-c2e9f0b7d635"],
      "text": "[Priya, 00:03:12] We should ship the beta on Friday.\n[Sam, 00:03:16] Friday works for me."
    }
  ],
  "reason": null,
  "unindexed_utterances": 1
}
```

No grounding (retrieval worked and found nothing relevant):

```json
{
  "query": {
    "query_id": "6b0d9e34-c2a7-4f18-95d3-7e1a4c8b0f52",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "mode": "live",
    "question": "Who is responsible for the budget?",
    "answer": null,
    "cited_chunk_ids": [],
    "status": "no_grounding",
    "created_at": "2026-09-21T11:41:20.005Z",
    "error": null
  },
  "citations": [],
  "reason": "no_relevant_evidence",
  "unindexed_utterances": 0
}
```

Failed (the system is broken, not "nothing found"):

```json
{
  "query": {
    "query_id": "0c7f2a95-d4e1-4b36-8a09-5f3b1d6e9c28",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "mode": "live",
    "question": "What did we decide about the beta launch date?",
    "answer": null,
    "cited_chunk_ids": [],
    "status": "failed",
    "created_at": "2026-09-21T11:42:10.870Z",
    "error": { "code": "retrieval_failed", "message": "searching the transcript failed" }
  },
  "citations": [],
  "reason": "retrieval_failed",
  "unindexed_utterances": 0
}
```

The most recent few seconds of speech may not yet be searchable because chunking runs slightly behind transcription (`rag-and-qa.md`); that is not an error. A meeting with no indexed content yet returns `no_grounding` when the index is healthy and `failed` when the embedding model or vector store cannot be used (gap X5, proposed, ADR-15).

**Status codes**

| Status | Code | When |
|---|---|---|
| 200 | — | `answered`, `no_grounding`, or `failed`; read `query.status` |
| 400 | `invalid_request` | empty or over-long question, or `mode` other than `live` |
| 404 | `meeting_not_found` | unknown meeting |
| 409 | `meeting_ended` | live Q&A on an ended meeting; use history mode (`POST /api/qa`) |
| 413 | `payload_too_large` | body over 64 KiB |
| 415 | `unsupported_media_type` | not JSON |

**Side effects** — inserts a `QAQuery`; audit `qa_query` (and `model_error` when a model call failed); one `ModelExecution` row for the question embedding and one for the answer generation, each with `related_id = query_id`; push `qa_answer` (the requester receives the HTTP response and may also receive the push, so clients dedupe by `query_id`). The push carries `query` and `citations` only. Not idempotent: asking again creates a new query. Q&A never blocks live transcription; if both compete for compute, STT has priority (`stt-pipeline.md`). Questions are answered one generation at a time; a second question waits for the first. If the `QAQuery` cannot be stored, the request fails with `500 internal_error` and no answer is returned.

### POST /api/qa

**Provisional (CON-14).** Ask across past meetings (`mode: history`), including a question about one ended meeting from the post-meeting view.

**Request** — `meeting_ids` is a non-empty array of meeting IDs, or null for all ended meetings.

```json
{ "question": "When did we last discuss the beta launch?", "meeting_ids": ["0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8"], "mode": "history" }
```

**Response** — `200` with the same `{ query, citations }` shape and the same three outcomes as the live endpoint. `query.mode` is `history`; `query.meeting_id` is set when exactly one meeting was searched and null otherwise (`data-model.md`). Each citation carries its own `meeting_id` so results can be told apart across meetings. The scope of a multi-meeting query is recorded in the audit payload (`qa_query.meeting_ids`), since `QAQuery` has no column for it; CON-14 may propose one.

**Status codes**

| Status | Code | When |
|---|---|---|
| 200 | — | `answered`, `no_grounding`, or `failed` |
| 400 | `invalid_request` | empty question, empty `meeting_ids` array, bad ID, or `mode` other than `history` |
| 404 | `meeting_not_found` | a listed meeting does not exist |
| 409 | `meeting_not_ended` | a listed meeting is still running |
| 413 | `payload_too_large` | body over 64 KiB |
| 415 | `unsupported_media_type` | not JSON |

**Side effects** — as for live Q&A; the push goes to the dashboard of each searched meeting that has one open.

**Not restored after reload.** No endpoint lists past `QAQuery` rows in the MVP, so a reloaded dashboard does not redisplay earlier answers (G18, proposed, ADR-15). Answers remain stored and in the audit stream; a list route can be added later without changing anything here.

### POST /api/meetings/{meeting_id}/summarize

Manually (re-)trigger summarization. It is also called automatically on meeting end (`summarization.md`).

**Request** — empty body or `{}`.

**Response** — `202`. Summarization runs in the background and the outcome arrives by push.

```json
{ "summary_pending": true, "summary_id": "b6d1e8a3-5c72-4f09-a3d4-8e0b2c7f1a56" }
```

**Status codes**

| Status | Code | When |
|---|---|---|
| 202 | — | attempt started |
| 404 | `meeting_not_found` | unknown meeting |
| 409 | `summary_in_progress` | an attempt is already running (the running attempt is unaffected; no duplicate starts) |
| 409 | `transcript_empty` | the meeting has no utterances |

**Side effects** — inserts a new `Summary` (`pending`); audit `summary_started` (`trigger: manual`). On success the row becomes `ready` with its `ActionItem` rows, audit `summary_generated`, push `summary_ready`; a parse failure after the one stricter retry, or a model error, makes it `failed` with an `error_message`, audit `summary_failed`, push `summary_failed`. A `ready` summary from an earlier attempt stays current until a newer `ready` one exists, so a failed re-run never erases a good summary. A `summarize` on a live meeting summarizes the transcript so far and does not end the meeting; unlike the end trigger it does not flush or wait for queued speech, so a sentence still being spoken is not cut off. The drain, the retry, the long-transcript limit and the `summary_failed` error codes are in `summarization.md`.

### GET /api/meetings/{meeting_id}/summary

Read the summary and action items for the post-meeting view and dashboard.

**Request** — no body.

**Response** — `200`. `summary` is the current `ready` summary or null; `latest_attempt` is the most recent attempt of any status. If a newer attempt failed after an earlier success, `summary` still holds the success and `latest_attempt.status` is `failed`, so the UI can show both. `action_items` belong to `summary`.

```json
{
  "summary": {
    "summary_id": "b6d1e8a3-5c72-4f09-a3d4-8e0b2c7f1a56",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "status": "ready",
    "summary_text": "The team agreed to ship the beta on Friday.\n\nRelease notes were assigned to Sam.",
    "error_message": null,
    "model_identifier": "mlx-community/example-reasoning-model",
    "generated_at": "2026-09-21T12:03:31.400Z"
  },
  "action_items": [
    {
      "action_item_id": "f0a3c5e7-1b92-4d68-8c4a-3e7d9b1f2a60",
      "summary_id": "b6d1e8a3-5c72-4f09-a3d4-8e0b2c7f1a56",
      "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
      "text": "Write the release notes",
      "owner_participant_id": "a8d20f6b-4c39-4e15-8b72-9e1c3d5a7f06",
      "status": "open",
      "owner_display_name": "Sam"
    },
    {
      "action_item_id": "0e9b4d61-7a35-4c82-b1f0-6d2c8a5e3b17",
      "summary_id": "b6d1e8a3-5c72-4f09-a3d4-8e0b2c7f1a56",
      "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
      "text": "Confirm the beta date with the client",
      "owner_participant_id": null,
      "status": "open",
      "owner_display_name": null
    }
  ],
  "latest_attempt": {
    "summary_id": "b6d1e8a3-5c72-4f09-a3d4-8e0b2c7f1a56",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "status": "ready",
    "summary_text": "The team agreed to ship the beta on Friday.\n\nRelease notes were assigned to Sam.",
    "error_message": null,
    "model_identifier": "mlx-community/example-reasoning-model",
    "generated_at": "2026-09-21T12:03:31.400Z"
  },
  "stale": false,
  "summary_pending": false,
  "as_of_seq": 431
}
```

A null `owner_participant_id` (shown as "Unassigned" in the export) is a normal outcome, not a failure. `stale` is true when utterances were created or corrected after the summary was built; the UI offers "regenerate", which calls `POST …/summarize`.

**Status codes**

| Status | Code | When |
|---|---|---|
| 200 | — | success |
| 404 | `meeting_not_found` | unknown meeting |
| 404 | `summary_not_found` | no summary attempt has ever been made for this meeting |

**Side effects** — none.

### GET /api/meetings/{meeting_id}/export

Download the DOCX minutes (`export.md`). This `GET` may do work: if no current, non-stale file exists it renders one first. Rendering is deterministic over stored data with no model call, so repeating the request is safe and yields the same content.

**Request** — query parameter `format`, default and only value `docx`.

**Response** — `200` with the file: `Content-Type: application/vnd.openxmlformats-officedocument.wordprocessingml.document` and `Content-Disposition: attachment; filename="<meeting_id>.docx"`. The file name is the meeting UUID, never derived from the title. The body is the binary DOCX, not JSON. A file rendered before a correction or a newer summary is re-rendered before it is served; an out-of-date file is never served silently. If that re-render fails the request fails with `export_render_failed`.

Errors use the JSON error shape:

```json
{ "error": { "code": "summary_not_ready", "message": "no ready summary exists for this meeting" } }
```

**Status codes**

| Status | Code | When |
|---|---|---|
| 200 | — | the DOCX |
| 400 | `unsupported_format` | `format` present and not `docx` |
| 404 | `meeting_not_found` | unknown meeting |
| 409 | `summary_not_ready` | no `ready` summary exists (none yet, pending, or only failed attempts) |
| 500 | `export_render_failed` | rendering failed; the summary and transcript are intact and a retry is safe |

**Side effects** — when a render happens: inserts an `Export` (`pending`, then `ready` with `storage_path` under `data/exports/`); audit `export_created` (its payload records `summary_id` and `input_as_of_seq`, from which staleness is derived); push `export_ready`. On failure: the `Export` becomes `failed` with an `error_message`; audit `export_failed`; push `export_failed`. Serving an existing current file writes nothing. The post-meeting view learns whether a file exists from `latest_export` in `GET /api/meetings/{meeting_id}` and from the pushes, without triggering a render.

### POST /api/meetings/{meeting_id}/devices/{device_id}/enroll

**Provisional (CON-13).** Enroll one speaker on a shared device (`speaker-attribution.md`). The audio is **captured by the server from the device's live WebRTC track** for the configured enrollment duration; the phone does not upload a recording (G19). The caller therefore has the speaker talk while the request is open.

**Request**

```json
{ "display_name": "Priya", "participant_id": null }
```

`display_name` names the speaker. `participant_id` is null for a first attempt; pass the earlier participant's ID to retry that participant's enrollment.

**Response** — `200` once the capture finished and an embedding was stored. A short or noisy sample is still stored, with `quality_flag = low_confidence_sample`, and later classification is treated as less reliable; it is not an error.

```json
{
  "participant": {
    "participant_id": "3e8b1d47-52a9-4c60-b7f2-0a9c6d4e8b15",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "device_id": "c1a44d90-8f27-4b13-9d6e-52f0a7b3c8e4",
    "display_name": "Priya",
    "enrollment_status": "enrolled",
    "color": "pink"
  },
  "enrollment": {
    "enrollment_id": "8a5c1e7d-3f92-4b06-a4d8-0e6b9c2f7a13",
    "participant_id": "3e8b1d47-52a9-4c60-b7f2-0a9c6d4e8b15",
    "sample_duration_s": 7.8,
    "enrolled_at": "2026-09-21T11:29:40.100Z",
    "quality_flag": "ok"
  }
}
```

**Status codes**

| Status | Code | When |
|---|---|---|
| 200 | — | enrolled, possibly flagged `low_confidence_sample` |
| 400 | `invalid_request` | missing or invalid `display_name` or `participant_id` |
| 404 | `meeting_not_found` | unknown meeting |
| 404 | `device_not_found` | unknown device |
| 409 | `device_not_shared` | the device is not shared |
| 409 | `device_not_connected` | the device has no live audio to capture |
| 409 | `enrollment_in_progress` | another capture is running on this device |
| 422 | `enrollment_low_quality` | no usable embedding could be produced; the participant is marked `failed` and falls back to the generic-label path |

**Side effects** — creates the `Participant` if needed, stores a `SpeakerEnrollment`, audit `enrollment_completed` or `enrollment_failed`, push `device_status`. The device leaves `enrolling` once all declared speakers have been enrolled or have failed.

## WebSocket: `/ws/signal/{meeting_id}`

Used by the phone client only. The message catalog, flows, error codes and reconnect rules are defined in `transport.md` ("Signaling", "Identity and reconnection"); this document does not repeat them. Summary: the phone registers over REST, then sends `join`, `offer` (optionally an ICE restart) and `leave`, and receives `joined`, `answer`, `error` and `meeting_ended`. ICE is non-trickle and there is no `ice-candidate` or `reconnect` message. Errors are per device: the offending device's socket and peer are reset and no other device is touched.

**Request** — WebSocket upgrade; an unknown meeting is reported by an `error` frame with code `meeting_not_found` and then a close, not by an HTTP failure, so the browser can read the reason.

**Response** — see `transport.md`.

**Status codes** — `101 Switching Protocols` on upgrade. Close codes: `1000` normal (including after `meeting_ended`), `4400` after a fatal `error`.

## WebSocket: `/ws/dashboard/{meeting_id}`

Used by the live dashboard and the post-meeting view. **Read-only:** the server pushes events; any text the client sends is ignored. There is no subscribe message; a connection is subscribed to its meeting on open. The server keeps the socket alive with WebSocket heartbeats and closes a client that cannot keep up with code `1013`; the client then resynchronizes.

**Request** — WebSocket upgrade. Unknown meeting: an `error` frame with code `meeting_not_found`, then a close.

**Response** — a stream of events, each an object with the envelope keys `type`, `seq`, `meeting_id` and `at` (server time when pushed) plus the payload keys below. `seq` is null for ephemeral events.

| type | durable | payload keys | Caused by |
|---|---|---|---|
| `meeting_status` | yes | `meeting` | meeting started or ended |
| `device_status` | yes | `device` | device registered, status changed, or its participants changed |
| `connection_event` | yes | `event` | a `ConnectionEvent` was recorded |
| `device_gauges` | no | `gauges` | periodic live health, about once per second while any device is connected |
| `utterance` | yes | `utterance` | a new transcript line |
| `utterance_updated` | yes | `utterance` | a correction or confirmation |
| `qa_answer` | yes | `query`, `citations` | a Q&A result of any status |
| `summary_ready` | yes | `summary_id` | a summary attempt succeeded |
| `summary_failed` | yes | `summary_id`, `error_message` | a summary attempt failed |
| `export_ready` | yes | `export_id`, `summary_id` | a DOCX was rendered |
| `export_failed` | yes | `export_id`, `error_message` | a DOCX render failed |
| `error` | no | `code`, `message` | the feed itself could not start (for example `meeting_not_found`) |

`utterance` and `utterance_updated` carry the complete `Utterance` with its derived fields, and `device_status` the complete `Device` with its participants, so a client replaces state rather than patching it. The existing names `utterance`, `connection_event`, `qa_answer` and `summary_ready` are kept.

<!-- example: ws=dashboard -->
```json
{
  "type": "meeting_status",
  "seq": 388,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T11:31:12.010Z",
  "meeting": {
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "title": "Sprint planning",
    "status": "live",
    "created_at": "2026-09-21T11:30:00.000Z",
    "started_at": "2026-09-21T11:31:12.000Z",
    "ended_at": null
  }
}
{
  "type": "device_status",
  "seq": 389,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T11:31:12.020Z",
  "device": {
    "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "joined_at": "2026-09-21T11:31:05.000Z",
    "status": "connected",
    "is_shared": false,
    "declared_speaker_count": 1,
    "reconnect_count": 0,
    "user_agent": "Mozilla/5.0 (Linux; Android 14) Chrome/128.0",
    "participants": [
      {
        "participant_id": "3e8b1d47-52a9-4c60-b7f2-0a9c6d4e8b15",
        "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
        "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18",
        "display_name": "Priya",
        "enrollment_status": "not_required",
        "color": "teal"
      }
    ]
  }
}
{
  "type": "connection_event",
  "seq": 389,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T11:31:12.020Z",
  "event": {
    "event_id": "4c8e2a90-b3d7-4f15-a6e1-9d0b5c7a3f28",
    "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "event_type": "connected",
    "timestamp": "2026-09-21T11:31:12.015Z"
  }
}
{
  "type": "device_gauges",
  "seq": null,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T11:34:16.000Z",
  "gauges": [
    {
      "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18",
      "last_audio_age_ms": 40,
      "audio_duration_s": 184.0,
      "stt_backlog": 0,
      "stt_dropped_windows": 0
    }
  ]
}
{
  "type": "utterance",
  "seq": 401,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T11:34:15.020Z",
  "utterance": {
    "utterance_id": "9c2a7e10-3b4d-4f6a-8e15-7a0d5c1b2e34",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18",
    "participant_id": "3e8b1d47-52a9-4c60-b7f2-0a9c6d4e8b15",
    "text": "We should ship the beta on Friday.",
    "t_start": "2026-09-21T11:34:12.400Z",
    "t_end": "2026-09-21T11:34:14.100Z",
    "stt_confidence": 0.93,
    "attribution_method": "device",
    "attribution_confidence": 0.95,
    "corrected": false,
    "original_participant_id": null,
    "created_at": "2026-09-21T11:34:15.020Z",
    "seq": 401,
    "speaker_label": "Priya",
    "low_confidence": false
  }
}
{
  "type": "utterance_updated",
  "seq": 412,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T11:41:07.552Z",
  "utterance": {
    "utterance_id": "1a6d3f82-9e07-4b5c-8a14-c2e9f0b7d635",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "device_id": "c1a44d90-8f27-4b13-9d6e-52f0a7b3c8e4",
    "participant_id": "a8d20f6b-4c39-4e15-8b72-9e1c3d5a7f06",
    "text": "I can take the release notes.",
    "t_start": "2026-09-21T11:34:16.900Z",
    "t_end": "2026-09-21T11:34:18.300Z",
    "stt_confidence": 0.88,
    "attribution_method": "manual_correction",
    "attribution_confidence": 1.0,
    "corrected": true,
    "original_participant_id": null,
    "created_at": "2026-09-21T11:34:19.240Z",
    "seq": 405,
    "speaker_label": "Sam",
    "low_confidence": false
  }
}
{
  "type": "qa_answer",
  "seq": 415,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T11:42:10.870Z",
  "query": {
    "query_id": "0c7f2a95-d4e1-4b36-8a09-5f3b1d6e9c28",
    "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
    "mode": "live",
    "question": "What did we decide about the beta launch date?",
    "answer": null,
    "cited_chunk_ids": [],
    "status": "failed",
    "created_at": "2026-09-21T11:42:10.870Z",
    "error": { "code": "retrieval_failed", "message": "searching the transcript failed" }
  },
  "citations": []
}
{
  "type": "summary_ready",
  "seq": 425,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T12:03:31.400Z",
  "summary_id": "b6d1e8a3-5c72-4f09-a3d4-8e0b2c7f1a56"
}
{
  "type": "summary_failed",
  "seq": 426,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T12:04:02.150Z",
  "summary_id": "5d3f7b19-0a84-4c26-9e51-b8c2a6d4f093",
  "error_message": "model output did not parse after one retry"
}
{
  "type": "export_ready",
  "seq": 428,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T12:03:33.020Z",
  "export_id": "d9c2a4f8-6e15-4b37-a0d9-1f5b7c3e8a24",
  "summary_id": "b6d1e8a3-5c72-4f09-a3d4-8e0b2c7f1a56"
}
{
  "type": "export_failed",
  "seq": 429,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T12:05:11.300Z",
  "export_id": "7e1a9c4b-2d63-4f80-b5a7-3c9d0e6f1b42",
  "error_message": "render failed"
}
{
  "type": "error",
  "seq": null,
  "meeting_id": "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8",
  "at": "2026-09-21T11:30:01.000Z",
  "code": "meeting_not_found",
  "message": "no meeting with id 0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8"
}
```

**Status codes** — `101 Switching Protocols` on upgrade. Close codes: `1000` normal, `1013` the client could not keep up (resynchronize), and `1011` an unexpected server failure. An unknown meeting is an `error` event followed by a normal close.

**Side effects** — none; the feed never changes state. Connections are not audited.

### Resynchronization

A dashboard that loads, reloads, or loses its socket has to rebuild its state without duplicate or missing lines and without a stale event overwriting fresher data (G9). Each snapshot response carries `as_of_seq`, and each durable event carries `seq`. The procedure:

1. Open `/ws/dashboard/{meeting_id}`. **Buffer** every event that arrives; do not apply any yet.
2. Fetch `GET /api/meetings/{meeting_id}` (devices, health, meeting status, summary and export state) and `GET /api/meetings/{meeting_id}/transcript`. Each response has its own `as_of_seq`.
3. Build state from each snapshot, then apply the buffered events to the state that snapshot produced, **discarding any durable event whose `seq` is less than or equal to that snapshot's `as_of_seq`** (the snapshot already includes it), and applying the rest in `seq` order. Events with `seq` null are ephemeral: apply the newest, discard older ones.
4. From then on apply events as they arrive. Because `utterance`, `utterance_updated` and `device_status` carry complete records, applying one is an upsert by `utterance_id` or `device_id`, which makes a repeat harmless.
5. On any socket close, keep showing the last state marked "stale", reconnect with backoff, and repeat from step 1. A server restart is handled the same way: `seq` is stored, so it never goes backwards.

The server reads a snapshot and its `as_of_seq` in one transaction, so the cursor is exactly consistent with the rows. The post-meeting view uses the same procedure and additionally the summary endpoint.

## Demo traceability

Every success criterion in `requirements.md` and every step in `demo.md` maps to a serving endpoint or event. A row that says "none" would be a gap; there are none in the MVP, and the should-have rows name their provisional routes.

| Source | What has to work | Served by |
|---|---|---|
| Criterion 1 — 2–10 phones join | Create a meeting, show the QR, phones register and connect, the dashboard shows them | `POST /api/meetings`, `GET /join/{meeting_id}`, `POST …/devices`, `WS /ws/signal/{meeting_id}`, `device_status`, `meeting_status` |
| Criterion 2 — live audio and per-phone state | Per-device state and audio activity | `GET /api/meetings/{meeting_id}`, `device_gauges`, `connection_event`, `device_status` |
| Criterion 3 — live attributed transcript | Lines appear with the right speaker within seconds | `utterance`, `GET …/transcript`, `speaker_label`, `low_confidence` |
| Criterion 4 — Wi-Fi interruption recovers as the same participant | Same identity, no meeting restart | `WS /ws/signal/{meeting_id}` `join` with `is_reconnect`, `connection_event`, `device_status` |
| Criterion 5 — live Q&A with citation | Grounded answer with who and when | `POST …/qa`, `qa_answer`, `citations` |
| Criterion 6 — summary, action items, DOCX | End the meeting, read the summary, download the file | `POST …/end`, `summary_ready`, `GET …/summary`, `export_ready`, `GET …/export` |
| Criterion 7 — later ask across meetings | List past meetings and ask across them | `GET /`, `GET /api/meetings`, `POST /api/qa` (provisional, CON-14) |
| Criterion 8 — shared phone, flagged and correctable | Enroll, low-confidence markers, correction | `POST …/enroll` (provisional, CON-13), `low_confidence`, `POST …/correct`, `utterance_updated` |
| Demo step 1 — the problem | Talking point | none needed; no system feature |
| Demo step 2 — phones join | As criterion 1 | as criterion 1 |
| Demo step 3 — live transcript | As criteria 3 and 4 | as criteria 3 and 4 |
| Demo step 4 — live Q&A | Ask about something said earlier, get a cited answer | `POST …/qa`, `qa_answer` |
| Demo step 5 — end and summary | As criterion 6 | `POST …/end`, `summary_ready`, `GET …/summary` |
| Demo step 6 — download export | The DOCX opens | `GET …/export` |
| Demo step 7 — offline claim | Everything ran with no internet | no endpoint; every asset is served locally from `/static/`, and the local-only test in `testing.md` verifies it |
| Failure recovery — phone will not connect | Fall back to the backup video | none; a recorded artifact (`demo.md`) |
