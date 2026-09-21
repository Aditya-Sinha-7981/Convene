# Manual tests (through CON-05)

This is the checklist for what the automated tests **cannot** establish: anything that depends on real phones, real browsers, real Wi-Fi, real microphones, and real speech. Everything built so far was tested with synthetic phones over loopback and synthetic speech. Until you run these, the transport, the reconnect behavior and the speech pipeline are **unproven on real devices**.

Record each result as **Passed**, **Failed** or **Not run (reason)** in `logs/transport.md` (Part A) and `logs/stt.md` (Part B), with the phone model, OS, browser and version. Do not put private audio or transcripts in those logs. If a check fails, write down exactly what you saw and which layer you think is at fault (see "Failure classes" at the end); do not add workarounds first.

**What exists today, so you know what you will and will not see:**

- Phones join from a QR code, register, stream audio, reconnect under the same identity, and leave.
- The laptop records every connection event in a database and shows devices and live health on a **minimal debug dashboard**.
- Each phone's speech is transcribed and printed **on the server terminal** (`STT [Name] #n ...`). There is **no** transcript screen, no speaker-attributed transcript yet, no Q&A, no summary, no export. Those are later tasks.

---

## 0. What you need

- The laptop (MacBook Pro M4 Pro), this repository, and its `.venv` (see `README.md`).
- **At least two phones**; three or more is better. Ideally one **Android with Chrome** and one **iPhone with Safari**, because they behave differently.
- A local Wi-Fi network with **no internet**. Any of these works; write down which you used:
  - a travel router with no WAN cable;
  - a phone's Personal Hotspot with mobile data **turned off** (the hotspot still lets devices talk to each other), with the laptop and the test phones joined to it (the hotspot phone itself is not a test phone);
  - the laptop's own hotspot (macOS Internet Sharing needs an uplink source, so this leaves the laptop online; only use it for the non-offline checks).
- A quiet room, plus a fan, music or TV to make noise for Part B.

## 1. One-time setup (needs internet once)

1. Dependencies and model:
   ```sh
   uv pip install -r requirements-dev.txt
   .venv/bin/python scripts/provision_models.py        # downloads about 1.6 GB, then verifies it offline
   ```
2. Local certificate authority (needed for the phones' microphones; browsers only allow the microphone on trusted HTTPS). This changes the laptop's trust store and asks for your password:
   ```sh
   mkcert -install
   mkcert -CAROOT        # prints the folder that holds rootCA.pem
   ```
3. Trust `rootCA.pem` **on each phone**. Get the file to the phone (AirDrop, email, a USB cable) and:
   - **iPhone:** open the file, install the profile (Settings > General > VPN & Device Management), then turn on full trust: Settings > General > About > Certificate Trust Settings > enable "mkcert ...".
   - **Android:** Settings > Security (or Privacy) > Encryption & credentials > Install a certificate > CA certificate, and pick the file.
   Do not use browser flags, tunnels or "proceed anyway" workarounds; if a phone will not trust the certificate, that is a result to record (failure class F2), not something to work around.
4. Connect the laptop and all phones to the same Wi-Fi and find the laptop's address:
   ```sh
   ipconfig getifaddr en0          # for example 192.168.50.10
   ```
5. Make a certificate for **that exact address** (do this again whenever the address changes; a changed hotspot address silently breaks phones):
   ```sh
   mkcert -cert-file local.pem -key-file local-key.pem 192.168.50.10
   ```

## 2. Starting the server

```sh
# Transport only (no model): use this for Part A, and any time you only want to check phones and Wi-Fi.
.venv/bin/python -m server.app --cert local.pem --key local-key.pem --advertise-ip 192.168.50.10 --no-stt

# With speech-to-text: use this for Part B.
.venv/bin/python -m server.app --cert local.pem --key local-key.pem --advertise-ip 192.168.50.10
```

Expected on the terminal: `LAN address: ...`, `Certificate OK`, (with STT) `STT model ready (<n> s)`, then `Open https://192.168.50.10:8443/ on this laptop ...`. If the certificate does not cover the address, or the model is not provisioned, the server refuses to start and says why; that is correct behavior.

**Starting a meeting:** on the laptop open `https://192.168.50.10:8443/` (use the address, not `localhost`), press **New meeting**. The dashboard page shows the join link and a QR code. On each phone scan the QR code (or type the link), enter a name, and tap **Join and start microphone**. Allow the microphone when asked.

## 3. How to look at what is happening

| What | How |
|---|---|
| Live health of each phone | the dashboard page (devices, status, reconnects, last audio age, audio seconds) |
| The same as JSON | `curl -sk https://192.168.50.10:8443/metrics \| python3 -m json.tool` (fields: `state`, `audio_received`, `last_audio_age_ms`, `frames`, `dropped_frames`, `audio_gaps`, and, with STT, an `stt` block: `windows_seen`, `windows_gated`, `windows_passed`, `enqueued`, `transcribed`, `empty`, `failed`, `dropped`, `suppressed`, `depth`, `oldest_age_s`, `vad_noise_floor_db`; in segment mode a "window" is one stretch of speech) |
| What the server heard | terminal lines like `STT [Priya] #3 11:34:12.400Z +2.7s conf=0.87 (queued 0 ms, model 480 ms): We should ship the beta on Friday.` |
| Connection history (the audit stream) | `sqlite3 data/convene.db "select seq, event_type, payload from AuditEvent order by seq desc limit 20"` (the file is `data/convene.db`) |
| Reconnect counts | `sqlite3 data/convene.db "select d.status, d.reconnect_count, p.display_name from Device d join Participant p using(device_id)"` |
| Model call timings | `sqlite3 data/convene.db "select duration_ms, related_id from ModelExecution order by created_at desc limit 20"` |

`last_audio_age_ms` is the time since the last audio arrived; it is **not** transport latency. Latency is not measured by anything today.

---

## Part A: transport and identity (CON-01 to CON-04)

Run these with `--no-stt`. They are the regression checklist R1 to R9 in `logs/transport.md`, which has never been run on phones.

| # | Test | How | Pass when |
|---|---|---|---|
| A0 | Startup refuses a wrong certificate | make a certificate for a different address and start the server with it | it exits with `certificate ... does not cover <address>` and the mkcert command to run |
| A1 | Join with no internet (R1) | disconnect the laptop from the internet, start a meeting, join from a phone | page loads, microphone permission is granted **without** any warning or workaround, `audio_received` becomes true and `frames` rises |
| A2 | One phone, 5 minutes (R2) | talk, pause, whisper, shout, count numbers for 5 minutes | `state` stays `connected`, `last_audio_age_ms` stays small, no manual restart. Record `audio_gaps` |
| A3 | Wi-Fi drop, 2 s (R3) | turn the phone's Wi-Fi off for 2 s, then on | the page shows "reconnecting" then "connected"; **same** name and device; `reconnect_count` goes up by 1; audit shows `device_reconnected`; other phones unaffected |
| A4 | Wi-Fi drop, 10 s (R4) | as A3 with 10 s | same as A3 (a short gap in audio is fine). The page retries with a growing delay (1 s, 2 s, 4 s, 8 s, 10 s); after **5 failed attempts in a row** it stops and shows a **Try again** button. Record whether a 10 s drop needed that tap |
| A5 | Two phones (R5) | two phones, each says a different phrase | two devices in `/metrics`, separate counters, no mix-up |
| A6 | One drops, others continue (R6) | with 3+ phones, turn one phone's Wi-Fi off | the others keep streaming; the dropped one returns under its old name |
| A7 | Close and reopen the tab (R7) | close the join page and open the same link again | **record what happens**: it should resume as the same device (identity is kept in the browser). If a new device appears, that is a finding for Android and iPhone separately |
| A8 | Screen lock and background (R8) | with audio flowing, lock the screen 10 s / 30 s / 1 min; then switch to another app 5 s / 30 s / 1 min | **observation only**: for each, record continued, paused, disconnected, browser killed, or needs a tap. Do it separately on each browser |
| A9 | Server restart with phones connected | press Ctrl+C on the server, start it again with the same command **within about 30 s** (after 5 failed retries a phone shows **Try again**; tapping it is acceptable, record it) | phones reconnect as the same devices; audit shows `device_disconnected` with reason `server_restart`, then `device_reconnected`; `reconnect_count` is 1 |
| A10 | End the meeting | press **End meeting** on the dashboard | phones show "The meeting has ended" and stop; devices become `left`; opening the old join link says the meeting ended |
| A11 | The Stop button | tap **Stop** on a phone | that phone stops; its device becomes `left`; joining again resumes as the same person |
| A12 | Dashboard reload | reload the dashboard page mid-meeting | devices and QR still show; state matches `/metrics` |
| A13 | HTTPS trust per platform | note every step needed to get the microphone working on each phone | record exact steps and any failure; no insecure workaround is acceptable |

## Part B: speech-to-text (CON-05)

Run these with the model (no `--no-stt`). Say the sentences at a normal pace. Write down what you said, and compare with the terminal lines.

| # | Test | How | Pass when |
|---|---|---|---|
| B0 | Start with no internet | turn the internet off, then start the server | `STT model ready`; no error; and `sudo lsof -iTCP -sTCP:ESTABLISHED -n -P \| grep -i python` shows no connection except your phones and the loopback |
| B1 | One phone, sentences with pauses | say five sentences, pausing 1 to 2 seconds between them | **one terminal line per sentence**, about 1 to 2 seconds after you finish; the text matches what you said. Note misheard words |
| B2 | Long run-on speech | talk for 30 seconds without stopping | a new line at least every 8 seconds, cut at a brief gap; **no words missing or repeated at the cut points** (compare with what you said) |
| B3 | Silence | say nothing for 2 minutes | **no lines**. A line such as "Thank you." is a failure to record. Check `windows_gated` rising in `/metrics` |
| B4 | Background noise | play a fan, music, TV or street noise near one phone, without speaking, for 2 minutes | ideally no lines. Record which noises produced false lines: this is the known weak spot (steady noise is gated; surging noise can get through) |
| B5 | Quiet and distant speech | whisper; speak from 3 m; speak with the phone in a pocket | record which of these are picked up, and which are missed |
| B6 | Two phones at once | two people speak different sentences at the same time | each line carries the **right name**; no line appears under the wrong person |
| B7 | Cross-device bleed | put two phones next to each other; only person A speaks | count lines that appear under B's name (`stt.transcribed` for each device in `/metrics`). This is the misattribution measurement (F6a): record the count and how far apart the phones were |
| B8 | Five phones talking (if you have them) | five phones, everyone talking | `stt.depth` stays small (under 5), `stt.dropped` stays 0 or tiny, lines keep appearing within a few seconds. Record the numbers |
| B9 | Alternating speakers | two phones, people take turns about a second apart | line times (in the terminal) are in the order people spoke |
| B10 | Reconnect mid-speech | speak a sentence, and turn the phone's Wi-Fi off just as you finish it | the sentence still appears (the open segment is sent when the stream ends), and lines resume after the phone reconnects |
| B11 | Timing | with a stopwatch, note the delay from the end of a sentence to its terminal line | roughly 1 to 2 seconds; record it |
| B12 | Resource use | Activity Monitor while five phones talk | the `python` process is around 2 GB of memory; record CPU and whether the fans run |
| B13 | If segments misbehave | edit `config/convene.toml`: `segmentation = "fixed"` and `window_ms = 3000`, restart | the fallback still works; compare the quality with B1 and B2 and record it |

## What to write down (a template)

| Field | Value |
|---|---|
| Date, who ran it | |
| Laptop / Wi-Fi method (router, phone hotspot with data off, ...) | |
| Phones: model, OS, browser and version | |
| Which tests passed / failed / not run, with the reason | |
| For failures: what you saw, and the log or `/metrics` output | |
| Numbers: `audio_gaps` in A2; B7 bleed count and distance; B8 depth and drops; B11 delay; B12 memory | |

## Failure classes (from `old_docs/TEST_PLAN.md` and `docs/testing.md`)

Name the layer before fixing anything. **F1** browser limitation (microphone or permission unavailable, stream stops when backgrounded). **F2** secure-context or certificate problem. **F3** Wi-Fi problem (drops at distance, congestion, or the access point isolating clients so phones cannot reach the laptop). **F4** transport problem (WebRTC will not connect, audio stops while Wi-Fi is fine, reconnect fails). **F5** laptop processing problem (CPU or memory saturation, STT backlog). **F6** misattribution (a line under the wrong person).

## If something does not work

- **"Microphone API unavailable"** on the phone: the certificate is not trusted (F2), or the page was opened over `http` instead of `https`.
- **The QR opens but the page does not load:** the phone is not on the same Wi-Fi, the laptop's address changed (regenerate the certificate and restart with the new `--advertise-ip`), or the access point blocks phone-to-laptop traffic.
- **Server exits at startup:** read its message; it names the certificate address problem or the missing model.
- **No `STT` lines while speaking:** check `stt.windows_seen`, `windows_gated` and `noise floor` in `/metrics`. If everything is gated, the phone is very quiet or the room noise is above the speech; if nothing is counted, audio is not arriving (check `audio_received`).
- **Clean up between runs:** delete `data/convene.db` (and the `-wal` and `-shm` files next to it) for a fresh start. It holds transcripts and meeting records; it is git-ignored, and you should never commit it or paste its contents into a log.
