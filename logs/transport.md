# logs/transport.md

> Workstream: transport (CON-01 baseline, CON-04 FastAPI migration)
> Status: CON-01 automated scope complete; **CON-01 hardware baseline blocked (no phones)**

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
