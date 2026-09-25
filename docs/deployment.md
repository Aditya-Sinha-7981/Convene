# Deployment

## Target environment

One laptop (MacBook Pro, M4 Pro, 24GB — `models.md`), running the single Python process (`architecture.md`, ADR-01), serving both phones and the dashboard over the local network. No internet connectivity required after setup.

## Network setup

1. A phone hotspot (or Mac Internet Sharing) provides the local network. It needs weak-but-working internet for the public DNS update and each phone's first hostname resolution; after resolution, page, WebSocket, WebRTC audio, and local models stay on the LAN.
2. The laptop joins that hotspot as a client. Phones join the same hotspot.
3. Before starting the server, the operator explicitly runs `scripts/update_dns.py`. It updates a DNS-only Cloudflare A record to the laptop's current hotspot IPv4 address. The server never reads Cloudflare credentials or calls the provider.
4. Server binds `0.0.0.0:<port>` (not only `127.0.0.1`) and uses the configured public hostname in the join URL/QR. The LAN IP is still detected and displayed for diagnostics.

## HTTPS setup (required for phone microphone access)

`getUserMedia()` requires a secure context on essentially all mobile browsers. The demo path uses a public DNS hostname and a publicly trusted certificate issued ahead of time with an ACME DNS-01 challenge; serve the certificate's **full chain** and private key from outside the repository. The domain/certificate one-time setup is [domain-setup.md](domain-setup.md); the network rationale and hotspot constraints are [network-and-https.md](network-and-https.md).

`mkcert` remains available only for developer-owned phone testing. It requires each test phone to install and trust the local CA and is not an acceptable participant flow.

## Runtime configuration

A single config file (or `.env`) holds:

- Resource-type → model mappings (`models.md`) — the one place model choice is ever configured.
- Cloud fallback credentials (Groq/Gemini API keys), only read if a fallback is explicitly activated (ADR-06) — absence of these keys must never break the default local path.
- Server port (`--port`), data directory paths (`[paths]` in `config/convene.toml`: `data/convene.db`, `data/exports/`; relative paths resolve from the repository root).

## Local models setup (one-time, before demo day)

The STT model is pinned (model and exact revision) in `[models.stt]` in `config/convene.toml`. Download it once, while online, and verify it the way the server will use it:

```sh
.venv/bin/python scripts/provision_models.py            # downloads the pinned model (about 1.6 GB), then verifies it offline
.venv/bin/python scripts/provision_models.py --check    # later: verify only, no network
.venv/bin/python scripts/provision_models.py --resource embedding
.venv/bin/python scripts/provision_models.py --resource embedding --check
```

The server never downloads anything: it resolves the pinned revision from the local Hugging Face cache with networking off, and refuses to start if it is missing. The embedding provisioning command prints the resolved revision; copy that exact value into `[models.embedding]` before the demo. To change models, edit the relevant `[models.*]` section (see `models.md`) and provision again.

- `mlx-whisper` model weights downloaded/cached locally ahead of time — do not rely on a model download happening live on unreliable Wi-Fi.
- Local `reasoning` LLM (via `mlx-lm`) weights similarly pre-downloaded and verified to load within the memory budget (`models.md`).
- Embedding and speaker-embedding models likewise pre-cached.
- Verify all of the above load and run **with the laptop's Wi-Fi disconnected from the internet**, matching the actual demo condition (inherited directly from DT-17's Test 0: Internet independence).

## Running the server

```sh
# Explicit preflight while the laptop is connected to the hotspot. This is the only Cloudflare API call.
.venv/bin/python scripts/update_dns.py

# Use certificate paths kept outside the repository. Do not pass the Cloudflare token to this process.
.venv/bin/python -m server.app --cert /outside/repo/fullchain.pem --key /outside/repo/privkey.pem \
  --public-host convene.example.com --advertise-ip 172.20.10.4 --port 8443
```

The server is one FastAPI process served by uvicorn over HTTPS on `0.0.0.0` (`ADR-01`). `--public-host` (or `[network].public_host`) becomes the host in join URLs/QR codes; `--advertise-ip` remains the actual laptop address used for diagnostics. Startup warns—but does not block—if the hostname resolves elsewhere, because the laptop resolver can differ from a phone's. It blocks for a mismatched, expired, or unreadable certificate. Then open `https://<public-host>:8443/` on the laptop, press **New meeting**, and have phones join the hotspot before scanning the meeting QR. The dashboard shows the join link/QR, device health, attributed live transcript, confidence/review state, and correction controls. Q&A, summary, and export are later tasks.

## Startup sequence

Implemented, in this order, before the server accepts connections:

1. Detect the LAN address (or take `--advertise-ip`) and print it. With no public host and no LAN address, the server starts but returns no join URL or QR code.
2. Load the certificate and key together, and fail loudly if the certificate has expired or its subject alternative names do not include the public hostname (or advertised IP in development mode). Warn when fewer than 14 days remain, a hostname certificate appears self-signed/private-CA, or the hostname does not resolve to the current LAN address.
3. Open `data/convene.db`, apply pending migrations, and reconcile after a restart (devices left `connected` or `joining` in an open meeting become `disconnected` with reason `server_restart`; meetings stay `live`, so phones reconnect as the same participant).
4. Load the STT model from the local cache and run a warm-up transcription (about 1.5 s once cached). If the model is not pinned, not cached, or fails to load, print why and exit; the server never starts half-working. `--no-stt` skips this step and runs the transport only (audio is received and counted, not transcribed), for checking phones and Wi-Fi without the model.
5. Record `model_load` in the audit stream, start the transcription pipeline, start the WebSocket hub and the periodic metrics log line, then serve.

Not yet implemented: loading the `embedding`, `speaker_embedding` and `reasoning` models (added by CON-08, CON-09 and CON-13).

## What is explicitly not part of deployment

Any cloud hosting, containerization for scale, CI/CD, or production secrets management — none apply to a single-laptop demo target (`requirements.md` non-goals).
