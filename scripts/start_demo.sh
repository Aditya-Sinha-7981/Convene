#!/usr/bin/env bash
# Start the complete Convene demo server from one reviewed command.
#
# Required environment: CONVENE_CERT and CONVENE_KEY.  Optional:
# CONVENE_ADVERTISE_IP, CONVENE_PUBLIC_HOST, CONVENE_PORT, CONVENE_CONFIG.
# See tests/manual-test/DEMO_FLOW.md before using this on demo day.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

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

echo "Starting Convene with local-only model mode enabled."
exec "$PYTHON" "${args[@]}"
