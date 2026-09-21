# Convene

Convene turns phones on a local Wi-Fi network into separate meeting microphones: a laptop receives each phone's audio over WebRTC, and will transcribe it, attribute speech by device, answer questions about the meeting, and produce a summary and DOCX minutes. **What exists today** is the transport, meeting and device registry, and persistence: phones join a meeting from a QR code, stream audio to a per-device sink, reconnect under the same identity, and the laptop records every connection event in an audit stream. Local speech-to-text (VAD, windowing, a fair scheduler and an `mlx-whisper` adapter) now turns each phone's audio into transcribed windows, but nothing yet turns those into speaker-attributed transcript lines. Attribution, the live dashboard, Q&A, summary and export are designed in `docs/` but **not implemented yet**. Nothing here has been verified on real phones; see `logs/transport.md`.

Read the [Convene documentation](docs/README.md) for the design and build plan, and `AGENTS.md` before changing anything.

## Install

Use Python 3.11–3.13 (the automated tests also pass on 3.14). Install while the Internet is available, then work offline:

```sh
uv venv --python 3.13
uv pip install -r requirements-dev.txt     # runtime dependencies plus pytest and the aiohttp test client
```

## Run

Download the speech model once while online (about 1.6 GB); the server never downloads anything and will not start without it:

```sh
.venv/bin/python scripts/provision_models.py
```

To check phones and Wi-Fi without the model, start the server with `--no-stt` (audio is received and counted, not transcribed).

Connect the Mac to the hotspot or local Wi-Fi. Find its address (`ipconfig getifaddr en0` on most Macs), generate a certificate covering that exact address, and trust the CA on each phone as described in [local HTTPS instructions](old_docs/HTTPS_LOCAL.md). Replace the example address:

```sh
mkcert -install
mkcert -cert-file local.pem -key-file local-key.pem 192.168.50.10
.venv/bin/python -m server.app --cert local.pem --key local-key.pem --advertise-ip 192.168.50.10
```

Open `https://192.168.50.10:8443/` on the Mac and press **New meeting**; the page shows a QR code and join link. Each phone scans it, enters a name, and taps **Join**. A QR code does not bypass HTTPS trust: the phone must trust the local CA before microphone access works. Regenerate the certificate if the Mac's hotspot address changes; the server refuses to start if the certificate does not cover the address it advertises.

The terminal logs connection state, audio, and a metrics line every 5 seconds. `https://<laptop-ip>:8443/metrics` returns the same per-device counters as JSON (a debug route, not part of the API contract). `last_audio_age_ms` is time since the last audio frame, **not** one-way latency. The microphone stays active while the join page is open; screen lock and background behavior must be measured on each browser.

Run details, the startup checks, and the database location are in [deployment](docs/deployment.md). The earlier DT-17 `aiohttp` prototype and its optional `STT_COMMAND` command-line transcription were replaced by this server and its speech pipeline, and are recoverable from Git history (commit `d44ba68`).

## Data

Meetings, devices, participants, utterances and the audit stream are stored in SQLite at `data/convene.db`, with exports under `data/exports/`; both paths come from `config/convene.toml`. `data/` is git-ignored because it will hold private transcripts. Never commit it or paste its contents into logs.

## Test

```sh
.venv/bin/python -m pytest tests -q
```

Tests run offline with no phones, certificates, or model weights. Tests that need the real speech model are marked `model` and skipped by default; run them on the reference laptop with `.venv/bin/python -m pytest tests -m model`.

With the model loaded, each phone's speech is printed on the server terminal as `STT [Name] #n ...` (one line per stretch of speech; `log_transcripts = false` in `config/convene.toml` turns it off). Per-phone counters are at `GET /metrics`.

**What to check by hand, step by step, with real phones:** [`docs/manual-tests.md`](docs/manual-tests.md).

The default run needs no weights (`tests/js/` runs the join page under `node`). They use synthetic phones over loopback, which show the server's protocol, identity and cleanup code work; they do **not** show browser, Wi-Fi, HTTPS-trust, screen-lock, or latency behavior. Those need real phones: the regression checklist R1 to R9 in `logs/transport.md`.
