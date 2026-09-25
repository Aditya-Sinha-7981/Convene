# Convene

Convene turns phones on a local Wi-Fi network into separate meeting microphones: a laptop receives each phone's audio over WebRTC, transcribes it locally, attributes each dedicated device's speech to its registered participant, and presents a live dashboard with correction. **What exists today** is the transport, meeting/device registry, local STT, dedicated-device attribution, correction audit trail, and dashboard: phones join from a QR code, stream audio to a per-device sink, reconnect under the same identity, and their attributed transcript rows appear live. Live grounded Q&A, summary, and DOCX export remain planned and are **not implemented yet**. A first real-phone trusted-host run has verified QR join, microphone capture, local transcription, device attribution, and live dashboard rendering; the broader Wi-Fi, reconnect, multi-device, noise, and language checklist remains open in [`docs/manual-tests.md`](docs/manual-tests.md) and the relevant `logs/` files.

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

For a zero-install phone run, join the laptop to the hotspot, run the explicit Cloudflare DNS preflight, then start with the publicly trusted hostname and pre-issued full-chain certificate:

```sh
.venv/bin/python scripts/update_dns.py
.venv/bin/python -m server.app --cert /outside/repo/fullchain.pem --key /outside/repo/privkey.pem \
  --public-host convene.example.com --advertise-ip 172.20.10.4
```

The preflight is an operator command, not server behavior: it updates the DNS-only Cloudflare A record to the laptop's current hotspot IP. A phone then needs only to join the hotspot, scan the meeting QR, enter a name, and allow microphone access—no profile or CA installation. Weak internet is required for the DNS update and first lookup, but all meeting traffic stays on the local hotspot. See [network and HTTPS](docs/network-and-https.md) and [domain setup](docs/domain-setup.md).

For developer-owned phones, IP + `mkcert` remains available; it requires manually trusting the local CA on each phone and is not the demo path. See [deployment](docs/deployment.md).

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
