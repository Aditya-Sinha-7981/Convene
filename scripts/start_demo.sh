#!/usr/bin/env bash
# Start the complete Convene demo server from one reviewed command.
#
# Required environment: CONVENE_CERT and CONVENE_KEY.  Optional:
# CONVENE_ADVERTISE_IP, CONVENE_PUBLIC_HOST, CONVENE_PORT, CONVENE_CONFIG.
# STT backend: CONVENE_STT=local (default) or CONVENE_STT=gemini with GEMINI_API_KEY and GEMINI_STT_MODEL
# (demo-only cloud connector, ADR-31). They may be kept in stt.env (gitignored; see stt.env.example). .env is
# never loaded here: it holds DNS credentials that must stay out of the server process.
# See tests/manual-test/DEMO_FLOW.md before using this on demo day.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

if [[ -f "$ROOT/stt.env" ]]; then
  set -a
  # shellcheck disable=SC1091
  source "$ROOT/stt.env"
  set +a
fi

: "${CONVENE_CERT:?Set CONVENE_CERT to the PEM certificate path.}"
: "${CONVENE_KEY:?Set CONVENE_KEY to the PEM private-key path.}"

PYTHON="${CONVENE_PYTHON:-$ROOT/.venv/bin/python}"
if [[ ! -x "$PYTHON" ]]; then
  echo "Demo startup failed: Python not found at $PYTHON. Create .venv first." >&2
  exit 2
fi
if [[ ! -r "$CONVENE_CERT" || ! -r "$CONVENE_KEY" ]]; then
  echo "Demo startup failed: CONVENE_CERT or CONVENE_KEY is unreadable." >&2
  exit 2
fi

# Model libraries must fail rather than silently trying to download during a demo.
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

args=( -m server.app --cert "$CONVENE_CERT" --key "$CONVENE_KEY" )
[[ -n "${CONVENE_ADVERTISE_IP:-}" ]] && args+=( --advertise-ip "$CONVENE_ADVERTISE_IP" )
[[ -n "${CONVENE_PUBLIC_HOST:-}" ]] && args+=( --public-host "$CONVENE_PUBLIC_HOST" )
[[ -n "${CONVENE_PORT:-}" ]] && args+=( --port "$CONVENE_PORT" )
[[ -n "${CONVENE_CONFIG:-}" ]] && args+=( --config "$CONVENE_CONFIG" )

if [[ "${CONVENE_STT:-local}" == "gemini" ]]; then
  echo "Starting Convene with CLOUD speech-to-text (Gemini, demo only). Other models stay local."
else
  echo "Starting Convene with local-only model mode enabled."
fi
exec "$PYTHON" "${args[@]}"
