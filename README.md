# Convene

Convene is a planned offline meeting assistant that uses a phone per speaker for reliable attribution, live transcription, Q&A, and meeting minutes. The code currently in this repository is its DT-17 transport prototype: phones send live microphone audio to a laptop over WebRTC, with optional local command-driven STT. The full Convene features described in the design documents are not yet implemented.

Read the [Convene documentation](docs/README.md) for the design and build plan. Read the [DT-17 test plan](old_docs/TEST_PLAN.md) before measuring the current prototype.

## Run

Use Python 3.11–3.13. Install dependencies while Internet is available, then run tests offline:

```sh
uv venv --python 3.13
uv pip install -r requirements.txt
```

Connect the Mac to the hotspot or local Wi-Fi first. Find its Wi-Fi IPv4 address with `ipconfig getifaddr en0` (on most Macs), then generate a certificate covering that exact address. Replace the example address below:

```sh
mkcert -install
mkcert -cert-file local.pem -key-file local-key.pem 192.168.50.10
```

Trust the mkcert CA on each phone as described in [local HTTPS instructions](old_docs/HTTPS_LOCAL.md). Then:

```sh
.venv/bin/python -m server.app --cert local.pem --key local-key.pem --advertise-ip 192.168.50.10
```

Open the printed join URL on each phone, or open the printed `join-<ip>.svg` file on the Mac and scan it. The QR encodes the local IP URL and works without Internet. Regenerate the certificate and QR if the Mac's hotspot IP changes. A phone must trust the local CA before microphone access works; a QR code does not bypass HTTPS trust.

The terminal reports connection state, audio windows, and recent audio age; `https://<laptop-ip>:8443/metrics` provides current values as JSON. `last_audio_age_ms` measures time since receipt, **not** one-way transport latency.

Local STT is optional. Set `STT_COMMAND` to a local program that reads a WAV file and writes text to stdout, using `{wav}` as the input placeholder. For whisper.cpp, for example:

```sh
STT_COMMAND='whisper-cli -m /path/to/ggml-model.bin -f {wav} -nt' .venv/bin/python -m server.app --cert local.pem --key local-key.pem
```

Download the model before the offline test. Without `STT_COMMAND`, audio windows still appear and no transcription is attempted. The microphone remains active while the page is open; screen lock and background behavior must be measured on each browser.

## Data

The Convene persistence layer (not yet wired into the prototype server) stores meetings, devices, participants, utterances and the audit stream in a SQLite file at `data/convene.db`, with exports under `data/exports/`; both paths come from `config/convene.toml`. `data/` is git-ignored because it will hold private transcripts. Never commit it or paste its contents into logs.
