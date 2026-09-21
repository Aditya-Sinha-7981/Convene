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
- Server port, data directory paths (`data/convene.db`, `data/exports/`).

## Local models setup (one-time, before demo day)

- `mlx-whisper` model weights downloaded/cached locally ahead of time — do not rely on a model download happening live on unreliable Wi-Fi.
- Local `reasoning` LLM (via `mlx-lm`) weights similarly pre-downloaded and verified to load within the memory budget (`models.md`).
- Embedding and speaker-embedding models likewise pre-cached.
- Verify all of the above load and run **with the laptop's Wi-Fi disconnected from the internet**, matching the actual demo condition (inherited directly from DT-17's Test 0: Internet independence).

## Startup sequence

1. Start the server process.
2. Confirm LAN address detected and displayed.
3. Confirm HTTPS certificate loads without error.
4. Confirm all local models load successfully (fail loudly here, not mid-meeting).
5. Display join URL/QR code.

## What is explicitly not part of deployment

Any cloud hosting, containerization for scale, CI/CD, or production secrets management — none apply to a single-laptop demo target (`requirements.md` non-goals).
