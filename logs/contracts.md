# logs/contracts.md

> Workstream: API and event contracts (CON-02)
> Status: drafted; **awaiting project-lead confirmation of the "Proposed" items below**

Tracked project history. No secrets, private audio or transcripts.

---

## CON-02 — Convene API and event contracts

### Summary (2026-09-21)

Documentation and contract work only; no server, client or storage code. Hardware and model checks: **none apply** to this task. Contract behavior on real phones and models is verified by the tasks that implement it.

| Deliverable | Where |
|---|---|
| Conventions, error catalog, derived fields, route index, served pages, all MVP endpoints, both WebSockets, resync procedure, demo traceability | `docs/api.md` |
| Final signaling messages, errors, flows, identity and peer lifetime | `docs/transport.md` |
| Conventions, `AuditEvent.seq`, complete audit catalog, `Summary`/`Export` status fields, derived state and gauges | `docs/data-model.md` |
| ADR-15 to ADR-18 (all **Proposed**) | `docs/decisions.md` |
| One sentence aligned to the settled Q&A semantics | `docs/rag-and-qa.md` |
| Documentation-consistency tests | `tests/test_api_contract_docs.py` |

No stored field was renamed (guarded by `test_no_stored_entity_field_was_renamed`). Four additions to stored entities are **proposed**: `AuditEvent.seq`, and `status` plus `error_message` on `Summary` and on `Export` (with `summary_text` and `storage_path` now nullable).

### Needs the project lead's confirmation

Each is written into the docs as the recommended resolution from the work order so CON-03 to CON-11 are not blocked. If one is rejected, amend the ADR and the documents it names together. Highest risk first:

1. **G13, schema.** `status` and `error_message` on `Summary` and `Export`; nullable `summary_text` and `storage_path`. (ADR-18)
2. **G9, schema.** `AuditEvent.seq` as the resync and ordering cursor. (ADR-18)
3. **G5, accepted limitation.** `device_id` is a bearer identifier; a LAN client that knows it can take over that device. No per-device secret. Visible through `device_reconnected` audit events. (ADR-15)
4. **G6 / X4, transport behavior change from the prototype.** Non-trickle ICE, one `join` for first attach and reconnect, and the signaling socket and the peer connection have separate lifetimes (closing the socket does not close the peer). Unproven on real phones until CON-04 re-runs R3 and R4 in `logs/transport.md`. (ADR-16)
5. **X3, casing.** All signaling fields are now `snake_case` (the earlier `transport.md` examples were camelCase). (ADR-16)
6. **G8, G18.** Q&A `no_grounding` and `failed` return HTTP 200 with the `QAQuery`; no list route, so a reloaded dashboard does not redisplay past answers. (ADR-15)
7. **G1.** UUID meeting ID in the URL and QR; no short join code. (ADR-15)
8. **G10.** Correction by `participant_id` or `display_name`; first original kept on the row; same-participant correction is allowed and means confirm. (ADR-17)
9. **X5.** An empty but healthy index answers `no_grounding`, not `failed`; this refines one sentence in `rag-and-qa.md`. (ADR-15)
10. **X7.** Ending a meeting with no utterances starts no summary (`summary_pending: false`); `POST …/summarize` on it is `409 transcript_empty`. CON-10 to confirm.

### Gap ledger

Status meanings: **Resolved** = the missing detail is now documented and changes no documented behavior or schema; **Proposed** = documented as the recommended resolution, needs confirmation (list above); **Deferred** = provisional shape documented, finalized by the named task. X-rows are gaps found while doing this work.

| # | Gap | Status | Owning document | Resolution |
|---|---|---|---|---|
| G1 | Meeting ID format | Proposed | `docs/api.md` (Conventions), `docs/data-model.md` | UUID stored, in URL and QR; no short code (ADR-15) |
| G2 | Join URL and QR payload | Resolved | `docs/api.md` (`POST /api/meetings`) | `join_url`, inline `qr_svg` encoding the URL only, `warnings: ["no_lan_address"]` with null join fields when no address is found; also returned by `GET …/{meeting_id}` for a reloaded dashboard |
| G3 | Two registration paths | Proposed | `docs/transport.md`, `docs/api.md` | REST registers; WebSocket `join` only attaches; unregistered `join` is `device_not_registered` (ADR-16) |
| G4 | Device and participant IDs | Proposed | `docs/transport.md`, `docs/data-model.md` | client UUID `device_id` in `localStorage` per meeting; server issues `participant_id` (ADR-16) |
| G5 | Reconnect identity and impersonation | Proposed | `docs/transport.md`, `docs/decisions.md` | accepted limitation, recorded (ADR-15) |
| G6 | Signaling message set | Proposed | `docs/transport.md` | both directions with examples; non-trickle; single `join`; `leave`, `meeting_ended`; fatal and non-fatal errors (ADR-16) |
| G7 | Dashboard push events | Resolved | `docs/api.md` | added `meeting_status`, `device_status`, `device_gauges`, `utterance_updated`, `summary_failed`, `export_ready`, `export_failed`, `error` |
| G8 | Q&A outcome versus error | Proposed | `docs/api.md`, `docs/rag-and-qa.md` | HTTP 200 with the persisted `QAQuery` for all three statuses (ADR-15) |
| G9 | Dashboard resynchronization | Proposed | `docs/api.md`, `docs/data-model.md` | `seq` from `AuditEvent.seq`, snapshot `as_of_seq`, subscribe-buffer-fetch-discard procedure (ADR-18) |
| G10 | Correction body and semantics | Proposed | `docs/api.md` | `participant_id` or `display_name`; first original kept; full from/to in the audit payload; confirm allowed (ADR-17) |
| G11 | Audit event catalog | Resolved | `docs/data-model.md` | 25 event types with payload keys; ConnectionEvent-to-audit mapping |
| G12 | Live metrics versus audit-as-truth | Resolved | `docs/data-model.md` ("Derived state and live gauges") | durable state derived from the stream; four ephemeral gauges never persisted |
| G13 | Summary and Export failure state | Proposed | `docs/data-model.md` | `status`, `error_message`; nullable `summary_text` and `storage_path` (ADR-18) |
| G14 | `GET …/export` side effects | Resolved | `docs/api.md` | 200 file, 400 `unsupported_format`, 404, 409 `summary_not_ready`, 500 `export_render_failed`; re-render when stale, never serve stale |
| G15 | Summarize and end responses | Resolved | `docs/api.md` | `end` 202 (200 if already ended) with `summary_pending`; `summarize` 202 creates a new `Summary`; latest `ready` is current; export auto-renders after a successful summary |
| G16 | Transcript endpoint | Resolved | `docs/api.md` | full ordered list, optional `after_seq`, derived `seq`, `speaker_label`, `low_confidence`, `as_of_seq` |
| G17 | History Q&A scope | Deferred to CON-14 | `docs/api.md` (`POST /api/qa`) | provisional `{question, meeting_ids: [..] or null, mode: "history"}`, ended meetings only |
| G18 | Q&A history after reload | Proposed | `docs/api.md` | no list route in the MVP; documented as a limitation (ADR-15) |
| G19 | Enrollment upload | Deferred to CON-13 | `docs/api.md` (`…/enroll`) | provisional: server captures from the device's live stream during the request |
| G20 | Confidence semantics for the UI | Resolved | `docs/api.md` (Derived fields) | server-owned threshold, boolean `low_confidence`, clients never compare numbers (ADR-17) |
| X1 | No route returned the summary or action items | Resolved | `docs/api.md` | added `GET /api/meetings/{meeting_id}/summary` |
| X2 | Served HTML routes undocumented | Resolved | `docs/api.md` (Served pages) | `/`, `/join/…`, `/dashboard/…`, `/meetings/…`, `/static/…` |
| X3 | camelCase signaling versus snake_case elsewhere | Proposed | `docs/transport.md` | `snake_case` throughout (ADR-16) |
| X4 | Peer lifetime tied to the signaling socket | Proposed | `docs/transport.md` | separate lifetimes so ICE restart can work (ADR-16) |
| X5 | Empty index: `failed` or `no_grounding` | Proposed | `docs/rag-and-qa.md`, `docs/api.md` | empty but healthy index is `no_grounding` (ADR-15) |
| X6 | Phone lifecycle messages absent | Proposed | `docs/transport.md` | `leave` and `meeting_ended` (ADR-16) |
| X7 | Ending a meeting with no transcript | Proposed | `docs/api.md` | no summary attempted; `409 transcript_empty`; CON-10 to confirm |

### Verification

```bash
.venv/bin/python -m pytest tests/test_api_contract_docs.py -q
.venv/bin/python -m pytest tests -q
```

The tests check, offline: every JSON example parses; every stored-entity example uses only fields from `docs/data-model.md` plus the documented derived fields, and enum values match; IDs are UUID v4 and timestamps are UTC with milliseconds; field names are `snake_case`; audit and WebSocket examples match their catalogs (including exact payload and envelope keys); every route in the index has request, response, status-code and side-effect sections and a worked example, with status codes consistent with the error catalog; every original stored field still exists; the demo traceability table covers criteria 1 to 8 and demo steps 1 to 7; every gap G1 to G20 is accounted for here.

Not verified by these tests, by design: that any route exists or behaves as documented.

### Manual implementability walk-through

Done by the author of the documents, so it is **not** an independent reader check; the work order's second-reader step (a fresh session sketching the join flow and resync client from the documents alone) is **Not run**.

Tracing the demo steps end to end through `docs/api.md` found and fixed one defect: a reloaded dashboard had no way to get the join URL or QR again (`POST /api/meetings` returns them once), so `GET /api/meetings/{meeting_id}` now returns `join_url` and `qr_svg`.

Known consequences left in the contract, not defects: a phone whose signaling socket is already gone when the meeting ends cannot receive `meeting_ended`; its next `join` gets `meeting_ended` as a fatal error and it should stop retrying. The dashboard has no "summary started" push; it learns `summary_pending` from the meeting-detail snapshot and the `summary_ready` or `summary_failed` push.

### Handoff

- **CON-03:** implement `AuditEvent.seq` in the single `emit` path; the 25-event catalog with payload keys; `Summary`/`Export` status and nullable fields; UTC-millisecond timestamps; idempotent `register_device` with `device_conflict` (cross-meeting and `is_shared` mismatch); staleness derivation from `input_as_of_seq`.
- **CON-04:** implement routes, signaling and errors exactly as documented; peer and socket lifetimes are separate (verify with R3 and R4); serve the pages and `/static/`; `join_url` and `qr_svg` on create and on meeting detail.
- **CON-06 / CON-07:** correction semantics (ADR-17), `speaker_label` and `low_confidence` as single server-side implementations, the resync procedure.
- **CON-09 / CON-10 / CON-11:** Q&A three-outcome mapping including X5; summary states and X7; export states, auto-render, and no stale serving.
- **CON-13 / CON-14:** finalize the provisional enrollment and history shapes.
- Choices left to tasks: the default title format's timezone display, the `stt_backlog` and gauge push period, and any confidence numbers (contract fixes semantics only).
