# Deployment

## Target environment

One laptop (MacBook Pro, M4 Pro, 24GB — `models.md`), running the single Python process (`architecture.md`, ADR-01), serving both phones and the dashboard over the local network. No internet connectivity required after setup.

## Network setup

1. Laptop hotspot (or a dedicated travel router/AP, evaluated later — nice-to-have, `requirements.md`) — no internet uplink required or expected.
2. Phones join that local Wi-Fi.
3. Server binds `0.0.0.0:<port>` (not only `127.0.0.1`); the join URL/QR uses the laptop's actual LAN address, auto-detected and displayed at process startup (`transport.md`). Do not hard-code an IP.

## HTTPS setup (required for phone microphone access)

`getUserMedia()` requires a secure context on essentially all mobile browsers. Local development approach:

1. Generate a local CA and certificate with `mkcert`, covering the laptop's LAN hostname/IP as it will actually be accessed.
2. Serve the app over HTTPS using that certificate.
3. Trust the CA on each test phone (Android and iOS trust steps differ — document exact steps as they're worked out, this is operational runbook content, not architecture).
4. If a phone refuses to trust the CA, document the exact failure rather than falling back to insecure browser flags or a public tunnel (ngrok, Tailscale, etc.) — both would violate the offline-first requirement (`requirements.md`).

## Runtime configuration

A single config file (or `.env`) holds:

- Resource-type → model mappings (`models.md`) — the one place model choice is ever configured.
- Cloud fallback credentials (Groq/Gemini API keys), only read if a fallback is explicitly activated (ADR-06) — absence of these keys must never break the default local path.
- Server port (`--port`), data directory paths (`[paths]` in `config/convene.toml`: `data/convene.db`, `data/exports/`; relative paths resolve from the repository root).

## Local models setup (one-time, before demo day)

- `mlx-whisper` model weights downloaded/cached locally ahead of time — do not rely on a model download happening live on unreliable Wi-Fi.
- Local `reasoning` LLM (via `mlx-lm`) weights similarly pre-downloaded and verified to load within the memory budget (`models.md`).
- Embedding and speaker-embedding models likewise pre-cached.
- Verify all of the above load and run **with the laptop's Wi-Fi disconnected from the internet**, matching the actual demo condition (inherited directly from DT-17's Test 0: Internet independence).

## Running the server

```sh
.venv/bin/python -m server.app --cert local.pem --key local-key.pem [--port 8443] [--advertise-ip 192.168.50.10] [--config config/convene.toml]
```

The server is one FastAPI process served by uvicorn over HTTPS on `0.0.0.0` (`ADR-01`). `--advertise-ip` is the address put in join URLs and QR codes; without it the server uses the default-route interface's private IPv4 address, falling back to the hostname lookup. On macOS the detection can return nothing or the wrong interface, so pass `--advertise-ip` (the address from `ipconfig getifaddr en0`) whenever it prints a warning or the wrong address. The certificate must cover exactly that address, and must be regenerated when the hotspot assigns a new one:

```sh
mkcert -install                                    # once, then trust the CA on each phone (old_docs/HTTPS_LOCAL.md)
mkcert -cert-file local.pem -key-file local-key.pem 192.168.50.10
```

Then open `https://<that address>:8443/` on the laptop, press **New meeting**, and have each phone scan the QR code. The dashboard page shows the join link, QR code, devices, and a raw event feed; the full dashboard is a later task.

## Startup sequence

Implemented, in this order, before the server accepts connections:

1. Detect the LAN address (or take `--advertise-ip`) and print it. With none, print a warning; the server still starts but returns no join URL or QR code.
2. Load the certificate and key together, and fail loudly if the certificate has expired or its subject alternative names do not include the advertised address. The message names the address and the names the certificate does cover.
3. Open `data/convene.db`, apply pending migrations, and reconcile after a restart (devices left `connected` or `joining` in an open meeting become `disconnected` with reason `server_restart`; meetings stay `live`, so phones reconnect as the same participant).
4. Start the WebSocket hub and the periodic metrics log line, then serve.

Not yet implemented: confirming that all local models load (added by CON-05 and later; fail loudly here, not mid-meeting).

## What is explicitly not part of deployment

Any cloud hosting, containerization for scale, CI/CD, or production secrets management — none apply to a single-laptop demo target (`requirements.md` non-goals).
