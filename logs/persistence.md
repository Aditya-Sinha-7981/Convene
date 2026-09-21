# logs/persistence.md

> Workstream: meeting, device, participant and audit persistence (CON-03)
> Status: implemented and tested offline; **not yet wired into `server/app.py`** (CON-04)

Tracked project history. No secrets, private audio or transcripts. The manual checks below used made-up names and a throwaway database that was deleted afterwards.

---

## CON-03 — Meeting, device, participant, and audit persistence

### Summary (2026-09-21)

| Item | Result |
|---|---|
| Migration runner (`PRAGMA user_version`, plain SQL) and `0001_core.sql` for the six entities | Done |
| Connection factory (foreign keys, WAL, busy timeout, rows by name) and concurrency model | Done |
| Repositories for `Meeting`, `Device`, `Participant`, `Utterance`, `ConnectionEvent`, `AuditEvent` | Done |
| `audit.emit` (single write path) and `record_connection_event` (atomic with its audit row) | Done |
| `register_device`, attach/detach with reconnect handling, restart reconciliation | Done |
| `config/convene.toml` + settings loader; `data/` git-ignored | Done |
| `sqlite-vec` spike | Done, all capabilities CON-08 needs are present (below) |
| Automated tests | **197 passed** (`tests/`, about 9 s, offline). The four CON-03 files plus config and spike: 121 tests |
| Mutation check | 16 deliberate breakages of the storage code were each caught by the tests |
| Manual: real `data/convene.db`, SIGKILL, restart | Passed (below) |
| End-to-end reconnect identity on real phones | **Not run — needs CON-04 wiring** and phones |
| `server/app.py`, `client/` | Untouched |

### Commands

```bash
.venv/bin/python -m pytest tests/test_db_migrations.py tests/test_repositories.py tests/test_audit_emit.py tests/test_device_registration.py -q   # the work order's command
.venv/bin/python -m pytest tests -q                                                                                                             # everything: 197 passed
```

### Environment

macOS, Python 3.14.7 (Homebrew), SQLite 3.53.4 (JSON1 present, `enable_load_extension` available), `sqlite-vec` 0.1.9. Stdlib only for storage; `tomllib` reads the config. **No dependency was added to `requirements.txt` or `requirements-dev.txt`.** `sqlite-vec` was installed into `.venv` for the spike only; `tests/test_sqlite_vec_spike.py` skips itself when it is absent. CON-08 must add it to `requirements.txt`.

### Files

New: `server/{db,audit,audit_catalog,registry,config,errors,ids,timeutil}.py`, `server/repositories/{__init__,base,models,meetings,devices,participants,utterances,connections,audit_events}.py`, `server/migrations/0001_core.sql`, `config/convene.toml`, `tests/{test_db_migrations,test_repositories,test_audit_emit,test_device_registration,test_config,test_sqlite_vec_spike}.py`, `tests/support/storage.py`. Modified: `.gitignore` (`/data/`), `README.md` (data note), `docs/data-model.md` (two clarifications), `tests/conftest.py` (`db` fixture).

`server/registry.py`, `errors.py`, `ids.py`, `timeutil.py` and `audit_catalog.py` are not in the work order's allowed-file list; they hold `register_device`/reconciliation and shared helpers the scope requires, and follow `docs/architecture.md` ("Participant/Device Registry").

### Decisions to know about

1. **Registration versus reconnect (reconciles the work order with CON-02).** The work order says a second `register_device` for a known device is a reconnect (`reconnect_count += 1`, `device_reconnected`). The CON-02 contract, which `AGENTS.md` makes authoritative over a task file, says the opposite: `POST …/devices` is an idempotent replay that writes nothing, and a reconnect is counted when the phone re-attaches over the WebSocket (`docs/api.md`, `docs/transport.md`). Implemented per the contract: `register_device` replays; `record_device_connected` counts reconnects. A device "has connected before" if it has any `connected`/`reconnected` `ConnectionEvent`, so a device that was registered but never connected (for example marked `disconnected` by reconciliation) is not a reconnect when it first connects. The work order's acceptance test ("second registration returns the same participant and increments `reconnect_count`") is covered as: replay returns the same participant with the count unchanged, and each subsequent attach increments it.
2. **`emit` takes the transaction:** `emit(tx, event_type, component, payload, meeting_id=None, *, timestamp=None)`, not the work order's `emit(event_type, component, payload, meeting_id=None)`. It has to run inside the caller's transaction for `record_connection_event` to be atomic and for subscribers to be notified only after commit.
3. **Payload keys are exactly the catalogued keys** (a nullable key is present with value `null`), the `component` must match the catalog, and `meeting_id` is required except for `server_started`, `model_load`, `model_error`, `signaling_error` and `qa_query`. The code catalog `server/audit_catalog.py` is asserted equal to the table in `docs/data-model.md` by a test. Payload keys make it structurally impossible to put transcript text in an audit row.
4. **`ConnectionEvent.event_id` equals its audit event's `event_id`**, which links the projection to its record. Documented in `docs/data-model.md`, together with the list of event types that may have a null `meeting_id`.
5. **Checks stricter than the field lists in the docs**, all derived from the notes in `docs/data-model.md`: `Utterance` needs `participant_id` unless its method is `generic_unresolved`; `original_participant_id` only when `corrected = 1`; `t_end >= t_start`; confidences in 0 to 1; `Device`: a non-shared device declares exactly 1 speaker; every timestamp must match `????-??-??T??:??:??.???Z`. **CON-06 must satisfy these**, in particular a correction must set `corrected` before `original_participant_id`.
6. **No `CHECK` on `AuditEvent.event_type`/`component`:** `emit` validates against the catalog, so a new event type never needs a table rebuild.
7. **Shared devices:** stored `enrolling` with no participants; on attach a device still `enrolling` stays `enrolling` (CON-13 moves it to `connected`). Speaker count for shared devices is 2 or 3 (`docs/api.md`), stricter than the work order's `>= 1`.
8. **`create_meeting`** lives in `registry.py` (default title from local time, `meeting_created` audit) because the audit event has to come from one place. `end_meeting` is **not** implemented here (it needs the STT drain and summary trigger; CON-04/CON-10).
9. **Missing config file falls back to defaults**, as specified, including when an explicit path is passed. A typo in a future `--config` flag would therefore silently use defaults; CON-04 may want to reject an explicit missing path.
10. `PRAGMA synchronous = NORMAL` with WAL: survives an application crash (verified by SIGKILL below), but an OS crash or power loss can lose the most recent committed transaction. Acceptable for a demo laptop; switch to `FULL` if that changes.

### Concurrency approach (Requirement 2)

One connection per `Database`, `check_same_thread=False`, guarded by a lock. `await db.run(fn)` runs `fn(tx)` on a dedicated single worker thread, so a slow write never blocks the event loop that receives audio; synchronous code uses `with db.transaction()`. Every transaction is `BEGIN IMMEDIATE`, so even two `Database` objects on one file serialize their writers (loser waits up to the busy timeout, then `DatabaseBusyError`); `seq` (`MAX(seq)+1`) is therefore collision-free. Each `run` is its own short transaction, so one device's failing write cannot block or poison another's. Subscribers are notified after commit, queued to the event loop from the worker thread in commit order.

Evidence (tests): 150 concurrent `db.run` emits get contiguous `seq` 1 to 150 and are delivered in that order; ten 50 ms writes take at least 0.45 s (serialized) while a 5 ms heartbeat coroutine keeps ticking; one failing write among three does not stop the others and does not consume a `seq`; two `Database` objects on one file with two threads emit 80 events with unique `seq`.

### `sqlite-vec` spike (Requirement 7)

Python 3.14.7 / SQLite 3.53.4 / `sqlite-vec` 0.1.9 (`vec_version()` = `v0.1.9`). All checked in `tests/test_sqlite_vec_spike.py`:

| Capability CON-08 needs | Result |
|---|---|
| Load the extension (`enable_load_extension`) | Works |
| `vec0` table, KNN with `MATCH ... AND k = n`, nearest first | Works |
| **TEXT primary key** (`chunk_id text primary key`) | **Supported** |
| **Partition key** (`meeting_id text partition key`) with `meeting_id = 'A'`, and with `IN ('A','B')` | **Supported**; no cross-meeting leakage |
| Metadata column filter (`kind integer`, `AND kind = 2`) | Supported |
| `distance_metric=cosine` | Supported |
| Update an embedding in place, delete a row | Works |
| Wrong vector dimension | Rejected (`Dimension mismatch ... Expected 3 dimensions but received 2`) |
| **Read a database that contains a `vec0` table without loading the extension** | **Fails: `no such module: vec0`** |

The last row is a consequence CON-08 must handle: the connection factory has to load `sqlite-vec` before any query touches a `vec0` table, including the migration that creates it, and plain tools such as the `sqlite3` CLI cannot read those tables. Not decided here: `connect()` currently loads nothing.

### Manual verification

On the real path `data/convene.db` (directory created by the code; deleted afterwards):

1. Migration ran; `sqlite3` CLI: `user_version` 1, `journal_mode` wal, `integrity_check` ok, `foreign_key_check` clean, six tables and nothing from later tasks.
2. Process A created a meeting, registered "Priya", connected the device, then **killed itself with SIGKILL** (exit code 137, no clean shutdown). Process B reopened the file: the device was still `connected` with its participant; `reconcile_after_restart` set it `disconnected` (audit `device_disconnected` with reason `server_restart`, then `server_started` with 1 device and 1 meeting); when the phone returned it was a reconnect (`reconnect_count` 1) with the same single participant. Audit trail seq 1 to 7: `meeting_created`, `device_registered`, `meeting_started`, `device_connected`, `device_disconnected`, `server_started`, `device_reconnected`.
3. `git check-ignore` confirms `data/` (including the `-wal` and `-shm` files) is ignored; nothing under `data/` appears in `git status`.

This proves the storage rules. It does not prove that a real phone keeps its identity across a Wi-Fi drop or a server restart.

### Handoff

- **CON-04:** wire `register_device` (REST), `record_device_connected` / `record_device_disconnected` (peer state), `record_connection_event` (for `audio_resumed`), `create_meeting`, and `await db.run(registry.reconcile_after_restart)` before accepting connections. Register the dashboard hub with `db.subscribe(...)`. Map the typed errors to the HTTP codes in `docs/api.md`: `ValidationError` to `invalid_request`, `MeetingNotFoundError`, `DeviceNotFoundError`, `MeetingEndedError` to `meeting_ended`, `DeviceConflictError` to `device_conflict`, `DatabaseBusyError` to 503/`internal_error`. Implement `end_meeting` (devices `left`, `device_left` audit, `meeting_ended`).
- **CON-05:** add the `ModelExecution` migration as `0002_*.sql` (numbering must stay contiguous).
- **CON-06:** use `repositories.utterances`; emit `utterance_created` through `audit.emit`; satisfy the utterance checks in decision 5. `audit_events.max_seq` gives the dashboard `as_of_seq`.
- **CON-08:** add `sqlite-vec` to `requirements.txt`, load it in `connect()` before migrations, use the spike results.
- The `Summary`/`Export` status fields from CON-02 are applied by CON-10/CON-11 with their migrations, per the work order.

### Open questions (answered by defaults, please confirm)

- Registration versus reconnect semantics (decision 1) follow CON-02, not the work order text.
- Reconciliation leaves `enrolling` devices alone (the work order lists only `connected` and `joining`).
- Meeting `started_at` is set on first successful connection, not registration.
