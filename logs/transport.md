# logs/transport.md

> Workstream: transport (CON-01 baseline, CON-04 FastAPI migration)
> Status: CON-01 automated scope complete, **hardware baseline blocked (no phones)**. CON-04 implemented and tested over loopback; **real-phone regression re-run not done (no phones)**

Tracked project history. No secrets, private audio or transcripts.

---

## CON-01 — Transport baseline and migration map

### Summary (2026-09-21)

| Item | Result |
|---|---|
| Test scaffolding (`pytest.ini`, `requirements-dev.txt`, `tests/`) | Done |
| Characterization tests, synthetic phone, loopback tests | Done — 39 passed, 3 consecutive runs, ~8 s, offline |
| Migration map (below) | Done, verified against code by line and docs by section |
| Regression checklist R1–R11 (below) | Done |
| Real-phone baseline (DT-17 Tests 0, 1, 6, 9, 11; 7, 8 observations) | **Not run — no phones, certificate trust or local network available in this session.** Stage 1 acceptance needs two phones; the task is **blocked** on this part and must be reported to the project lead. |
| `server/`, `client/` behavior | Unchanged (`git status` shows only new files) |

Nothing here shows the transport works on a phone. The loopback tests show the server's receive and cleanup code works against an aiortc peer; they cannot show browser, Wi-Fi, HTTPS, screen-lock or latency behavior.

### Environment for the automated run

* macOS (Darwin 25.6.0), Python 3.14.7 in `.venv`. Note: `README.md` says use Python 3.11–3.13; the suite passes on 3.14.7.
* aiortc 1.15.0, aiohttp 3.14.3, av 17.1.0, pytest 8.4.2, pytest-asyncio 1.4.0.
* Loopback ICE connected over the machine's own interface addresses (a `192.168.x.x` host address and IPv6). aiortc is configured with `iceServers=[]`. The loopback tests were not run on a machine with no non-loopback interface; treat "runs with Wi-Fi fully off" as **not verified**.

### Commands

```bash
uv pip install --python .venv/bin/python -r requirements-dev.txt   # needs network once
.venv/bin/python -m pytest tests -q                                # default run is -m "not model"
.venv/bin/python -m pytest tests/test_prototype_signaling.py tests/test_prototype_loopback.py -q
```

Result: `39 passed` (three runs). No test needs a phone, certificate or model weights.

### Dev dependencies added (confirm with project lead)

`requirements-dev.txt`: `pytest>=8,<9`, `pytest-asyncio>=0.24,<2` (plus `-r requirements.txt`). Not in runtime `requirements.txt`. Task open question "is adding pytest/pytest-asyncio acceptable" is unanswered; the recommended default (yes, separate file) was used.

### Test harness decisions

* `server/app.py` has no app factory and CON-01 may not change it. `tests/conftest.py::build_prototype_app` drives the real `main()` with `web.run_app`, `ssl.SSLContext`, QR generation and LAN detection patched out and captures the resulting app. The routes under test are exactly the registered routes. It also asserts nothing tries to write a QR file.
* The server fixture uses `web.AppRunner(app, handler_cancellation=False)`. **`aiohttp.test_utils.TestServer` forces `handler_cancellation=True`**, which cancels the signaling handler mid-`close_peer` on a client disconnect (leaving `audio_task` set) — behavior `web.run_app` does not have (default `False`). Found and corrected during this task (first version of the loopback tests failed on it). CON-04 tests must not use `TestServer` for the same reason.
* The synthetic phone (`tests/support/synthetic_phone.py`) keeps all protocol handling in `SyntheticPhone` (`open/join/offer/receive/send/close`) so CON-04 changes one class. Track source is a generated tone or a 16-bit PCM WAV (`wav_path=`, linear-resampled to 48 kHz).

### Findings (observed; none fixed here)

1. **Garbage SDP is answered, not rejected.** `offer` validation checks only `type` and `len(sdp) <= 100000` (`server/app.py:166`). `sdp="this is not sdp"` returns an empty `answer` (no `m=audio`) and leaves a peer in `new` with no audio task. Target (`docs/transport.md`, Failure isolation) wants malformed input handled at the transport layer. Owner: CON-04. Pinned by `test_garbage_sdp_is_answered_with_an_empty_answer_not_an_error`.
2. **`receive_age_ms` in the `AUDIO` log is not latency.** `participant["last_audio"]` is set at `app.py:85` and read at `app.py:99` in the same iteration; it measures ~0 ms by construction (asserted ≤ 5 ms in `test_one_second_windows_are_logged_and_receive_age_is_not_latency`). Do not report it as evidence. Likewise `/metrics` `lastAudioAgeMs` is time since last receipt, not one-way latency. **Latency: not measured** (no valid method exists; out of scope for CON-01).
3. **Participant records are never evicted.** `participants` is a module-level dict (`app.py:26`); entries survive socket close and there is no meeting lifecycle. Pinned by `test_closing_the_socket_detaches_it_but_keeps_the_participant_record`. Owner: CON-03/04.
4. **Peer failure is only logged.** The `connectionstatechange` handler (`app.py:171-173`) logs and takes no action; recovery depends on the client reconnecting. Owner: CON-04 (`ConnectionEvent`).
5. **Any meeting path is accepted.** `/ws/{meeting}` and `/join/{meeting}` do no validation; `--meeting` (default `TEST-123`) only names the printed URL (`app.py:243, 262`). Owner: CON-02/04.
6. **Duplicate `join` on one socket** counts as a reconnect and keeps the socket open (`app.py:154-159`, `old_ws is ws`). Not in the docs. Pinned.
7. **Binary WebSocket frames are silently ignored** (`app.py:136-137`); non-object JSON (`null`, `[]`, `42`) reaches the generic `except Exception` and yields an `error` reply, and `log.exception` is called (`app.py:186-188`). Socket stays open in every malformed case. Pinned.
8. **`/metrics` is unauthenticated** and lists every participant to any LAN client. Acceptable for the prototype (ADR-10: no auth); note for CON-04.
9. **`audioDurationSeconds` is `samples / rate` using the current `rate`** (`app.py:212`); if a stream changes rate mid-session the total mixes rates. Buffers are cleared on a rate change (`app.py:79-84`) and no test exercises it.
10. **Receive errors are swallowed.** `receive_audio` catches all exceptions and logs `AUDIO [id] stopped: <message>` (`app.py:114-115`); a track end and a real error look the same (message is empty for a normal end).
11. **`close_peer` outcome:** closing the peer ends the track, so the audio task finishes by itself; `task.cancel()` then hits a finished task. No receive loop is left running (pinned; passes with `handler_cancellation=False`).
12. **Client identity is per-tab.** Token is in `sessionStorage` under `dt17:<meeting>:token` (`client/app.js:2-7`); closing the tab and reopening creates a new participant. Not exercised on a phone. Owner: CON-02 (target: `device_id` persisted for the session; storage choice to be decided).

### Migration map

Prototype cells were checked against `server/app.py` (`A:`) and `client/app.js` (`C:`) by line. Target cells were checked against `docs/transport.md`, `docs/api.md`, `docs/data-model.md`, `docs/stt-pipeline.md`, `docs/frontend.md`, `docs/deployment.md`. Where they disagree I only record it; CON-02 decides.

| Area | Prototype (verified) | Target (verified) | Verdict | Owner |
|---|---|---|---|---|
| Server framework | `aiohttp` `web.Application` (A:248), `web.run_app` with an `ssl.SSLContext` (A:246-247, 267) | FastAPI + aiortc, one process (ADR-01) | change | CON-04 |
| Signaling route | `GET /ws/{meeting}` (A:251) | `/ws/signal/{meeting_id}` (api.md) | change | CON-02 (path), CON-04 |
| Join page route | `GET /join/{meeting}` → `client/index.html` (A:249, 197-198); `GET /app.js` (A:250) | Join page surface in frontend.md §1; **route not defined in api.md** | keep path unless CON-02 decides otherwise; document it | CON-02 |
| Join message | `{type:"join", token}`, token is a 16–128 char string (A:141-146) → `{type:"joined", participantId, reconnects}` (A:163) | `{type:"join", meetingId, deviceId, isShared, declaredSpeakerCount}` (transport.md) | change | CON-02, CON-04 |
| Reconnect | Client re-sends `join` with the same token; server increments `reconnects`, closes old socket and peer (A:154-159; C:25-34, 62-68) | `{type:"reconnect", meetingId, deviceId}`; ICE restart first, fresh peer second; `Device.reconnect_count` + `device_reconnected` audit event | change (add ICE-restart-first) | CON-02, CON-04 |
| ICE | Non-trickle: client waits for gathering (5 s cap) then sends one offer (C:49-60); server has no `ice-candidate` handler → `error: unexpected message` (A:182-183, pinned) | `ice-candidate` message listed (transport.md) | **conflict — CON-02 decides** (keep non-trickle vs add trickle) | CON-02 |
| Identity | Client token in `sessionStorage` `dt17:<meeting>:token` (C:2-7); server ID `p`+6 hex in module dict keyed `(meeting, token)` (A:26, 146-153) | Client-generated `device_id` persisted for the session; `Device`/`Participant` rows (transport.md, data-model.md) | change | CON-02, CON-03 |
| Meeting | Any path value accepted (Finding 5) | `Meeting` with `created → live → ended` (data-model.md) | add | CON-03, CON-04 |
| Malformed message | Sends `{type:"error"}`, socket stays open (A:184-188); client treats error as retry (C:79-82) | Reject at transport layer; reset that device's connection only (transport.md) | change | CON-04 |
| Peer failure | Logs state only (A:171-173) | Mark device `disconnected`, write `ConnectionEvent` (transport.md, data-model.md) | add | CON-03, CON-04 |
| Metrics | `GET /metrics` JSON: `participant, meeting, state, audioReceived, lastAudioAgeMs, audioDurationSeconds, reconnects` (A:205-214); 5 s `METRICS` log line (A:217-226) | State, last-audio-age, reconnect count, total duration, **audio gaps** (transport.md); feeds dashboard + `ConnectionEvent` | change; **gaps absent** (pinned by `test_metrics_report_no_audio_gap_field_yet`) | CON-04 |
| Windowing | 1 s windows by sample count; times are monotonic seconds since stream start (A:90-99) | ~1 s windows, monotonic per-device `window_id`, ISO 8601 `t_start`/`t_end` (stt-pipeline.md) | change (`window_id`, UTC timestamps) | CON-05 |
| VAD | None | Per-device VAD gate (stt-pipeline.md) | add | CON-05 |
| STT | Optional `STT_COMMAND` subprocess per window, `Semaphore(1)` per participant, drops a window when 2 pending, 30 s timeout (A:27, 42-52, 100-111; stt.py:14-39) | Worker pool, bounded fair queue, fixed `transcribe_window` contract (stt-pipeline.md) | change | CON-05 |
| QR | Writes `join-<ip>.svg` into the repo root at startup, git-ignored (A:263-266; `.gitignore`) | Returned by `POST /api/meetings` (api.md) | change | CON-04 |
| LAN address | `local_addresses()` via `getaddrinfo(gethostname())` (A:30-39), or `--advertise-ip` (A:254-256); prints a message if none (A:259-260) | Detected at startup and displayed; no hard-coded IP (transport.md, deployment.md) | keep; add startup cert-coverage check (deployment.md) | CON-04 |
| Dashboard | None | `/ws/dashboard/{meeting_id}` (api.md) | add | CON-04 (hub), CON-07 |
| Audio capture | `getUserMedia({audio:true, video:false})`: default echo cancellation, noise suppression, AGC (C:94) | Unspecified in the docs; affects VAD/STT | add a decision (constraints) | CON-02, CON-05 |

Behavior the prototype has that the docs do not mention: exponential client retry 1 s → 10 s (C:13, 33); retry on `online` and `visibilitychange` (C:114-117); `WebSocketResponse(heartbeat=15)` (A:131); SDP length cap 100000 (A:166); the old socket is closed on rejoin (A:156-158); Stop button and mic-ended handling (C:98, 102-113); `/metrics` route (A:252); ignored binary frames (Finding 7); no server-side eviction (Finding 3).

Behavior the docs require that the prototype lacks: `Meeting`/`Device`/`Participant`/`ConnectionEvent` persistence; `device_reconnected` audit; ICE-restart-first recovery; audio-gap metric; `ice-candidate` and `reconnect` message types; consent line and name field on the join page (frontend.md §1); dashboard feed; QR from an API; certificate-coverage startup check.

### DT-17 baseline results (real phones)

Environment fields still to be recorded on the run: phone model, OS version, browser/version, Wi-Fi band, laptop model, access point/hotspot method, how the network was created with no uplink.

| Check | Source | Result |
|---|---|---|
| Internet independence (page load, mic permission, audio arrives, no outbound requests) | Test 0 | **Not run (no phones, no trusted mkcert certificate in this session)** |
| HTTPS trust on each phone without insecure flags | HTTPS_LOCAL | **Not run (same)** |
| One phone, 5 minutes | Test 1 | **Not run (no phone)** |
| Wi-Fi interruption 2 s / 10 s | Test 6 | **Not run (no phone).** Server-side identity reuse and reconnect counting were exercised over loopback only. |
| Two phones, unique phrases | Test 9 | **Not run (no phones).** Loopback with two synthetic phones shows separate participant IDs and separate counters — **not evidence** for phones. |
| One phone fails, others continue | Test 11 | **Not run (no phones).** Same loopback caveat. |
| Screen lock / background | Tests 7, 8 | **Not run** — observations per browser (Android Chrome, iPhone Safari) still needed |
| STT windows | Test 3 | **Not run** — `STT_COMMAND` not configured |
| Latency | Test 2 | **Not measured** — no valid one-way method |
| Dedicated-AP comparison | plan | Out of scope for CON-01 |

Also to check on the run (per task): whether any request leaves the LAN (fail if so), and that `AUDIO ... receive_age_ms` reads ~0 on phones as it does over loopback (mark "not evidence").

### Regression checklist for CON-04 (R1–R11)

Re-run after the FastAPI migration. The "baseline" column is **empty until the real-phone baseline is captured**; CON-04 must not treat a missing baseline as a pass. R10–R11 are automated and can run now.

| # | Check | Pass condition | Baseline (CON-01) |
|---|---|---|---|
| R1 | Offline join (Test 0) with internet unavailable, HTTPS trusted | Page loads, mic permission granted without insecure flags, audio reaches the laptop, no outbound request | Not run |
| R2 | One phone, 5 min (Test 1) | Continuous audio windows, `RECEIVING`, rising duration, no manual restart | Not run |
| R3 | Wi-Fi off 2 s (Test 6) | Same participant/device ID returns, reconnect count +1, no server restart, audio resumes | Not run |
| R4 | Wi-Fi off 10 s (Test 6) | Same as R3 | Not run |
| R5 | Two phones, unique phrases (Test 9) | Separate IDs and streams, no cross-contamination | Not run |
| R6 | One phone drops, others continue (Test 11); 5 phones preferred | Others keep streaming; the dropped phone returns under its old ID | Not run |
| R7 | Close and reopen the phone tab | Record what happens to identity (prototype: new participant, Finding 12); compare with the CON-02 target | Not run |
| R8 | Screen lock 10/30/60 s and backgrounding 5/30/60 s, per browser (Tests 7, 8) | Observation only: continued / paused / disconnected / killed / needs interaction | Not run |
| R9 | Malformed message from one phone while another streams | The other phone's audio and transcript are unaffected; the malformed sender is reset | Not run (loopback equivalent covered by R10) |
| R10 | `.venv/bin/python -m pytest tests -q` retargeted to the new signaling contract | All pass; keep every characterization intent (identity reuse, reconnect count, isolation, cleanup on close) | 39 passed (prototype) |
| R11 | Real-server-config check: tests must serve with `handler_cancellation=False` semantics (not `TestServer`) | Cleanup on disconnect runs to completion | Passed (prototype) |

### Open items for the project lead

* Provide two phones (Android Chrome + iPhone Safari preferred), a trusted mkcert setup and a no-uplink network, or accept the CON-01 hardware baseline as a deferred check. Stage 1 acceptance requires two phones.
* Confirm dev dependencies (`pytest`, `pytest-asyncio`).
* CON-02 decisions this log raises: ICE non-trickle vs trickle; where the join page route is specified; `sessionStorage` vs `localStorage` for `device_id`; mic constraints.


---

## CON-04 — FastAPI transport and meeting integration

### Summary (2026-09-22)

| Item | Result |
|---|---|
| One FastAPI + aiortc process over HTTPS on `0.0.0.0`; the `aiohttp` server is gone | Done |
| Signaling `/ws/signal/{meeting_id}` with the full CON-02 message set, per-device isolated sessions | Done |
| `POST /api/meetings`, `GET /api/meetings/{id}`, `POST …/devices`, `POST …/end`; served pages; `on_meeting_ended` hook | Done |
| Dashboard hub `/ws/dashboard/{meeting_id}` as an `emit` subscriber | Done: `meeting_status`, `device_status`, `connection_event`, `device_gauges` |
| Audio sink seam, `audio_resumed`, per-device gap and drop counters | Done |
| LAN detection, join URL, QR, certificate-coverage startup check | Done |
| Join page (name, consent line, persisted `device_id`, registration, reconnect, retry button) | Done |
| Synthetic phone retargeted; integration tests | Done |
| Automated tests | **302 passed**, four consecutive runs, about 40 s, offline. New for CON-04: 144 (`test_signaling` 40, `test_meetings_api` 34, `test_transport_integration` 28, `test_join_page` 18 under node, `test_audio_frames` 12, `test_network` 12). The work order's three files: 102 passed |
| Mutation check | 20 deliberate breakages of the server and the join page were each caught |
| Real process check (`python -m server.app`, TLS, curl) | Passed (below) |
| **CON-01 regression checklist on real phones (R1 to R9)** | **Not run: no phones, no trusted mkcert certificate, no local network in this session.** Parity with the prototype is **not** declared |

Nothing here shows the transport works on a phone. The synthetic phone is aiortc over loopback: it exercises the server's protocol, identity, isolation and cleanup code, not browser WebRTC, microphone permission, HTTPS trust, Wi-Fi loss, or screen lock.

### Run command and environment

`.venv/bin/python -m server.app --cert local.pem --key local-key.pem [--port 8443] [--advertise-ip IP] [--config PATH]` (the prototype's flags still work; `--meeting` is gone because meetings are created through the API). macOS, Python 3.14.7. Documented in `README.md` and `docs/deployment.md`.

Dependencies (the work order asked for these to be listed and confirmed; you approved adding an ASGI server):

| Package | Version tested | Where |
|---|---|---|
| `fastapi` | 0.141.1 | `requirements.txt` |
| `uvicorn[standard]` | 0.53.0 (with `websockets` 17.1, `uvloop` 0.22.1, `httptools` 0.8.0) | `requirements.txt` |
| `cryptography` | 50.0.1 | `requirements.txt` (aiortc already pulls it in; now used directly to read the certificate's names) |
| `aiohttp` | 3.14.3 | **moved to `requirements-dev.txt`**: only the test client and synthetic phone use it |

No STUN/TURN, tunnel, CDN asset or analytics. FastAPI's interactive docs pages are disabled because they load assets from a CDN (`/docs`, `/redoc`, `/openapi.json` are 404, tested). `starlette`'s `TestClient` needs `httpx2`, which was not added: tests serve the real app under uvicorn on a loopback port instead.

### Decisions and deviations

1. **ICE restart is not supported, so the server refuses it.** aiortc 1.15 has no ICE-restart code, and an empirical check showed that a second offer on a connected peer is accepted but the server keeps the old ICE connection and the new client connection fails. An `offer` with `ice_restart: true` is answered with the non-fatal `renegotiation_failed` (or `no_active_peer`), and the page always builds a fresh peer (the work order's recommended baseline, and what the prototype did). `docs/transport.md` now says so. The message field and `via: ice_restart` remain for a future server.
2. **Socket and peer lifetimes are separate (ADR-16, still Proposed).** Closing the signaling socket alone leaves a connected peer running and the device `connected`; status follows the peer (tested). A socket that closes with no connected peer releases the peer. The work order's sentence "or a closed socket: mark the device disconnected" is therefore followed only when there is nothing left connected. Unproven on real phones: this is exactly what R3 and R4 test.
3. **A reconnect is counted when the new peer connects,** not at `join`. `joined.reconnect_count` is the stored value at join time and `is_reconnect` says the device has connected before. Consequence, tested both ways: if the page closes its old peer before the new one arrives, the server records `connected`, `disconnected` (reason `peer_closed`), `reconnected`; on a silent drop it records `connected`, `reconnected`.
4. **Audio sink is async and fed through a bounded per-device queue** (250 frames, about 5 s). `push(device_id, pcm, sample_rate, t_wall)` is awaited from a per-device pump task, never from the receive loop. A slow sink fills only that device's queue; frames are dropped and counted. Tested with a sink that hangs forever for one device while another keeps streaming. Sinks must not run heavy synchronous work on the event loop.
5. **Time base available to CON-05:** `t_wall` is the server's wall clock in UTC epoch seconds at frame receipt: includes network and jitter-buffer delay, not sample-accurate. The frame's RTP-derived `pts` is available and is not passed on yet. `audio_duration_s` sums each frame's own duration, so a sample-rate change no longer skews it (prototype finding 9).
6. **Two audit additions** because the work order requires them to be audited and nothing in the catalog fit: `hook_failed {hook, error}` (new, in `docs/data-model.md` and `server/audit_catalog.py`), and receive-loop failures reuse `signaling_error` with code `receive_failed` (documented in `docs/transport.md`). Please confirm both.
7. **`end_meeting` and `record_device_left`** were added to `server/registry.py` (the CON-03 hand-off said end was CON-04's). One transaction; teardown and hooks run after it commits. Hooks are cut off after 5 s and a failing or hanging hook is audited without failing the request. `summary_pending` is always `false` until CON-10.
8. **Shutdown records nothing.** A clean stop marks sessions as closing, so devices stay `connected` in the database; the next start reconciles them (`server_restart`), which is what the restart test proves.
9. **`GET /metrics` kept** as a read-only debug route (the work order's open question default), now snake_case (`device_id`, `audio_received`, `last_audio_age_ms`, `frames`, `dropped_frames`, `sink_errors`, `audio_gaps`); it is not in `docs/api.md`'s contract and nothing may depend on it. The periodic 5 s `METRICS` log line is kept.
10. **DB busy maps to HTTP 500 `internal_error`** ("the database is busy; try again") because the error catalog has no 503.
11. **Pages:** `/` is a minimal "New meeting" page and `/dashboard/{id}` a minimal raw-feed page (CON-07 replaces it). `/meetings/{id}` (post-meeting view) and `GET /api/meetings` (list) are not served yet, so the ended-meeting redirect from the dashboard lands on a 404. `docs/api.md`'s status line says exactly what exists.
12. **Removed with the prototype:** optional command-driven STT (`STT_COMMAND`) no longer works; `server/stt.py` is untouched and unused until CON-05 replaces it. `join-<ip>.svg` is no longer written (the QR is returned by the API).

### Findings

* aiortc ICE restart (decision 1).
* uvicorn's own `ws_max_size` (1 MiB) caps a frame before the application sees it; the application limit is 128 KiB and answers `invalid_message`.
* FastAPI 0.141 wraps included routers lazily; route inspection through `app.routes` shows `_IncludedRouter`, so route checks were done against the running server.
* A plain `http://` request to the TLS port fails at the connection (curl exit 52), as expected; a phone that opens the `http` URL sees a failed load, not a redirect. No HTTP-to-HTTPS redirect was added.
* Two test-side issues found and fixed: aiohttp's client only processes a close frame when something reads the socket; a test that closed the phone before stopping the server measured a disconnect the real restart would not.

### Where the CON-01 characterization intents moved

| CON-01 test intent (prototype) | Now covered by |
|---|---|
| join validation, offer before join, malformed and binary frames | `test_signaling.py` (invalid device id, `not_joined`, `invalid_message`, unknown types, oversize) |
| same identity reused, reconnect counted, second socket replaces first | `test_transport_integration.py` (reconnect both patterns, second socket), `test_signaling.py` |
| two devices are two participants | `test_signaling.py` |
| metrics shape, `disconnected` with no peer | `test_meetings_api.py` (gauges, null before audio), `/metrics` test |
| cleanup on socket close, peer closed, audio task ended | `test_transport_integration.py` (socket close keeps the peer; peer close releases and records; `leave`) |
| isolation: one phone fails, others continue | `test_transport_integration.py` (malformed message and disconnect on B; receive-loop exception on B) |
| audio arrives through the real receive path | `test_transport_integration.py` (sink sees mono PCM), `test_audio_frames.py` (formats) |

### Real-process check (`python -m server.app`, TLS, throwaway self-signed certificate; `mkcert -install` was not run)

Passed: a certificate not covering the advertised address exits with `startup failed: certificate … does not cover 127.0.0.1; it covers: 192.168.1.5 …` and the mkcert command to run; with a covering certificate it printed the LAN address, `Certificate OK` and the URL, served `/` (200), `POST /api/meetings` returned a join URL and a QR (10 652 characters of SVG), the join page carries the consent line, `/docs` is 404, SIGINT exited 0 with a clean shutdown, and the database written by the real process holds `server_started` then `meeting_created`.

### Regression checklist (R1 to R11) after the migration

Compare each against the CON-01 baseline, which itself was never captured on phones.

| # | Check | CON-04 result |
|---|---|---|
| R1 | Offline join with HTTPS trusted | **Not run (no phones, no trusted certificate)** |
| R2 | One phone, 5 min | **Not run** |
| R3 | Wi-Fi off 2 s | **Not run.** Server behavior for a silent drop is covered over loopback only |
| R4 | Wi-Fi off 10 s | **Not run** |
| R5 | Two phones, unique phrases | **Not run** |
| R6 | One phone drops, others continue | **Not run.** Loopback isolation tests pass |
| R7 | Close and reopen the tab | **Not run.** The page now keeps `device_id` in `localStorage`, so a reopened tab should resume the same device; unverified on Android Chrome and iPhone Safari |
| R8 | Screen lock and backgrounding | **Not run** |
| R9 | Malformed message from one phone while another streams | Loopback equivalent passed; **not run on phones** |
| R10 | Tests retargeted to the new signaling contract | **Passed** (302 passed; intents mapped above) |
| R11 | Tests must model the real server's disconnect handling | **Passed, and re-derived:** the tests now run the real app under uvicorn, which has no aiohttp `TestServer` cancellation artifact |

Also not run and required by the work order: server restart while real phones are connected, HTTPS trust and microphone permission on each platform, and comparison with CON-01 results.

### Handoff

- **CON-05:** implement `AudioSink` (`server/transport/audio.py`), pass it to `create_app(sink=...)`; the queue and drop counters exist already. Add the `ModelExecution` migration as `0002_*.sql`.
- **CON-06:** the dashboard hub's `_translate` in `server/dashboard_hub.py` is where `utterance` and `utterance_updated` are added.
- **CON-07:** replace `client/dashboard.html`/`dashboard.js`; use the resync procedure in `docs/api.md`. `GET /api/meetings/{id}` already returns `as_of_seq`.
- **CON-10:** subscribe with `app.state.runtime.on_meeting_ended(hook)`; keep the hook fast and start the work as a task; serve `/meetings/{id}`.
- **CON-12:** re-run transport checks in the offline rehearsal.
- **Docs that are now stale and were not edited (outside this task's files):** the "checked-in `server/` and `client/` are the DT-17 prototype" sentences in `AGENTS.md`, `docs/README.md` and `docs/00-AI-CONTEXT.md`.


### Correction (2026-09-22)

The first version of the CON-04 summary above said "187 new tests" and `test_audio_frames` 10. Both were unchecked estimates. Counted with `pytest --collect-only`: 144 new tests, `test_audio_frames` 12. The suite total (302) and the work order's three-file count (102) were measured and were right. The summary row has been corrected in place.

---

## CON-04B — Trusted public hostname and hotspot DNS preflight

### Implementation (2026-09-25)

- Added optional trusted-host mode: `--public-host` overrides `[network].public_host`; otherwise the existing IP
  join URL and mkcert development flow remain unchanged.
- The server validates DNS SANs (including one-label wildcards), expiry, and OS hostname resolution. Certificate
  mismatch/expiry remains fatal; near-expiry, private-CA-looking hostname certificates, and stale/unavailable DNS
  are explicit warnings.
- Added the explicit operator command `scripts/update_dns.py`. It reads ignored `.env` credentials, updates exactly
  one DNS-only Cloudflare A record to the current private LAN IP, and is never imported or invoked by the server.
  No secret values are logged.
- This hotspot design needs weak internet for the operator DNS update and initial phone DNS lookup. WebRTC, page,
  WebSocket, STT, and dashboard traffic remain local after resolution. It is not an offline claim.

### Automated verification

- Initial focused suite (including config coverage): **108 passed**. Final focused suite after hostname meeting-URL
  coverage: `.venv/bin/python -m pytest tests/test_meetings_api.py tests/test_network.py tests/test_update_dns.py
  tests/test_transport_integration.py -q` — **86 passed**.
- `.venv/bin/python -m pytest tests -q` passed (exit 0) after the final test-only addition.
- `.venv/bin/python scripts/update_dns.py --help` passed, validating the documented direct script launch without
  reading `.env` or calling Cloudflare.
- Cloudflare calls are mocked; no provider API request, real certificate, phone, or model was used by these tests.

### Hardware checks

- **Not run:** public certificate issuance/path validation, explicit DNS update against the configured zone, Android
  Chrome zero-install join, iOS Safari zero-install join, hotspot DNS behavior, DNS-rebinding/Private-DNS behavior,
  and audio reception. These require the actual hotspot, certificate files, and phones.
