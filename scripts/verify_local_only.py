#!/usr/bin/env python3
"""Sample a server process's established TCP peers and reject non-local endpoints.

Usage: .venv/bin/python scripts/verify_local_only.py --pid <server-pid>

This samples ``lsof``; it cannot prove that no packet was ever sent. Run it throughout each
physical rehearsal while the Wi-Fi has no Internet route, and retain its terminal output in
the rehearsal record (but not private transcript data).
"""
from __future__ import annotations

import argparse
import ipaddress
import subprocess
import sys
import time
from collections.abc import Iterable


def peer_from_lsof_name(name: str) -> str | None:
    """Return the remote host from an lsof TCP NAME field, or None for listeners/unparseable rows."""
    if "->" not in name:
        return None
    remote = name.rsplit("->", 1)[1].split(" ", 1)[0].strip()
    if remote.startswith("["):
        return remote[1:].split("]", 1)[0]
    # IPv4 host:port and ordinary hostnames use the final colon as the delimiter.
    return remote.rsplit(":", 1)[0] if ":" in remote else remote


def is_local_peer(host: str) -> bool:
    """Accept loopback, private RFC1918/ULA, and link-local LAN addresses only."""
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False  # lsof should emit numeric addresses with -n; a hostname is suspicious.
    return address.is_loopback or address.is_private or address.is_link_local


def non_local_peers(lsof_output: str) -> list[str]:
    """Extract disallowed peers from ``lsof -nP -iTCP`` output."""
    blocked: list[str] = []
    for line in lsof_output.splitlines()[1:]:  # first line is the lsof column header
        if "->" not in line:
            continue
        # Do not rely on column counts: lsof variants differ in whether they print a NODE
        # column. The TCP NAME field follows the protocol token and precedes its state.
        name = line.split(" TCP ", 1)[-1].split(" (", 1)[0]
        peer = peer_from_lsof_name(name)
        if peer is not None and not is_local_peer(peer):
            blocked.append(peer)
    return blocked


def sample(pid: int) -> str:
    result = subprocess.run(
        ["lsof", "-nP", "-a", "-p", str(pid), "-iTCP", "-sTCP:ESTABLISHED"],
        capture_output=True, text=True, check=False,
    )
    # lsof exits 1 when the process simply has no matching connections.
    if result.returncode not in (0, 1):
        raise RuntimeError(result.stderr.strip() or f"lsof exited {result.returncode}")
    return result.stdout


def run_samples(pid: int, count: int, interval_s: float, reader=sample) -> Iterable[list[str]]:
    for number in range(1, count + 1):
        blocked = non_local_peers(reader(pid))
        yield blocked
        if number < count:
            time.sleep(interval_s)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pid", type=int, required=True, help="PID printed by the demo server shell")
    parser.add_argument("--samples", type=int, default=12, help="number of samples (default: 12)")
    parser.add_argument("--interval", type=float, default=5.0, help="seconds between samples (default: 5)")
    args = parser.parse_args(argv)
    if args.samples < 1 or args.interval < 0:
        parser.error("--samples must be at least 1 and --interval must be non-negative")
    try:
        for number, blocked in enumerate(run_samples(args.pid, args.samples, args.interval), 1):
            if blocked:
                print(f"FAIL sample {number}/{args.samples}: non-local TCP peer(s): {', '.join(blocked)}", file=sys.stderr)
                return 1
            print(f"PASS sample {number}/{args.samples}: established TCP peers are local or none")
    except RuntimeError as exc:
        print(f"FAIL: could not inspect PID {args.pid}: {exc}", file=sys.stderr)
        return 2
    print("PASS: sampled connections were local only (sampling is not a packet-level proof).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
