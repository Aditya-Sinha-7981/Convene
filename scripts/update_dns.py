#!/usr/bin/env python3
"""Explicit operator preflight: update the DNS-only Cloudflare A record for the laptop's current LAN IP.

This script is intentionally not imported by the server.  It may contact Cloudflare only when an operator runs it
before a hotspot session; the FastAPI process never reads these credentials or calls a DNS-provider API.
"""
import argparse
import json
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

# ``python scripts/update_dns.py`` places ``scripts/`` on sys.path, not the repository root.
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import network

API_ROOT = "https://api.cloudflare.com/client/v4"
REQUIRED = ("CONVENE_DOMAIN", "CLOUDFLARE_API_TOKEN", "CLOUDFLARE_ZONE_ID", "CLOUDFLARE_DNS_RECORD_NAME")


class PreflightError(Exception):
    """The explicit DNS update could not be completed safely."""


def load_env(path: Path) -> dict[str, str]:
    """Load simple KEY=VALUE secrets without executing the file as shell code."""
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise PreflightError(f"cannot read {path}: {exc}") from exc
    values: dict[str, str] = {}
    for number, raw in enumerate(lines, 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" not in line:
            raise PreflightError(f"{path}:{number}: expected KEY=VALUE")
        key, value = line.split("=", 1)
        if not key.replace("_", "a").isalnum() or not key or key[0].isdigit():
            raise PreflightError(f"{path}:{number}: invalid variable name")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
            value = value[1:-1]
        values[key] = value
    missing = [key for key in REQUIRED if not values.get(key)]
    if missing:
        raise PreflightError(f"{path}: missing required values: {', '.join(missing)}")
    if values["CLOUDFLARE_DNS_RECORD_NAME"] != values["CONVENE_DOMAIN"]:
        raise PreflightError("CLOUDFLARE_DNS_RECORD_NAME must equal CONVENE_DOMAIN for the single-host setup")
    network.validate_public_host(values["CONVENE_DOMAIN"])
    return values


def request_json(method: str, path: str, token: str, *, payload: dict | None = None, timeout_s: float = 15.0) -> dict:
    """Cloudflare API call that never includes the bearer token in an error or output."""
    data = None if payload is None else json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(API_ROOT + path, data=data, method=method, headers={
        "Authorization": f"Bearer {token}", "Content-Type": "application/json",
    })
    try:
        with urllib.request.urlopen(request, timeout=timeout_s) as response:
            body = json.loads(response.read().decode("utf-8"))
    except (urllib.error.URLError, urllib.error.HTTPError, OSError, ValueError) as exc:
        raise PreflightError(f"Cloudflare DNS request failed: {exc}") from exc
    if not body.get("success"):
        messages = "; ".join(error.get("message", "unknown error") for error in body.get("errors", []))
        raise PreflightError(f"Cloudflare DNS request was rejected: {messages or 'unknown error'}")
    return body


def update_record(values: dict[str, str], address: str, *, dry_run: bool = False) -> str:
    """Create or replace exactly one DNS-only A record. Return ``created`` or ``updated``."""
    address = network.detect_lan_address(address)  # validates that it is IPv4
    domain, zone, token = values["CONVENE_DOMAIN"], values["CLOUDFLARE_ZONE_ID"], values["CLOUDFLARE_API_TOKEN"]
    query = urllib.parse.urlencode({"type": "A", "name": domain})
    listed = request_json("GET", f"/zones/{zone}/dns_records?{query}", token)
    records = listed.get("result", [])
    if len(records) > 1:
        raise PreflightError(f"Cloudflare has {len(records)} A records named {domain}; keep exactly one")
    payload = {"type": "A", "name": domain, "content": address, "ttl": 60, "proxied": False}
    if dry_run:
        return "would update" if records else "would create"
    if records:
        request_json("PUT", f"/zones/{zone}/dns_records/{records[0]['id']}", token, payload=payload)
        return "updated"
    request_json("POST", f"/zones/{zone}/dns_records", token, payload=payload)
    return "created"


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(description="Update the configured DNS-only Cloudflare A record before a Convene run")
    parser.add_argument("--env", type=Path, default=Path(".env"), help="operator secret file (default: .env)")
    parser.add_argument("--advertise-ip", help="laptop IPv4 on the hotspot; auto-detected when omitted")
    parser.add_argument("--dry-run", action="store_true", help="validate settings and report the intended mutation only")
    args = parser.parse_args(argv)
    try:
        values = load_env(args.env)
        address = network.detect_lan_address(args.advertise_ip)
        if address is None:
            raise PreflightError("no private LAN IPv4 detected; join the hotspot or pass --advertise-ip")
        outcome = update_record(values, address, dry_run=args.dry_run)
    except (PreflightError, ValueError, network.HostnameError) as exc:
        sys.exit(f"DNS preflight failed: {exc}")
    print(f"Cloudflare DNS {outcome}: {values['CONVENE_DOMAIN']} -> {address} (DNS-only, TTL 60 s)")
    print("Start Convene with --public-host " + values["CONVENE_DOMAIN"])


if __name__ == "__main__":
    main()
