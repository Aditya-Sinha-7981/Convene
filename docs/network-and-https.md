# Network & HTTPS

## Status

This document is authoritative for how phones actually reach the laptop and get a trusted HTTPS connection. It **supersedes** the original `mkcert` + local-CA approach described in the prototype's `old_docs/HTTPS_LOCAL.md`. The travel-router + local-DNS-override approach considered earlier in planning has been **dropped entirely, not kept as a fallback** — locked decision, see `decisions.md` ADR-20. Do not resurrect either without updating this document and that ADR first.

## The problem this solves

Two independent requirements were in tension (see `decisions.md` ADR-20 for the full reasoning):

1. Mobile browsers require a secure context (HTTPS with a **trusted** certificate) for `getUserMedia()` — a self-signed/local-CA certificate requires manual install/trust on every phone, which fails the "scan and go" requirement outright, especially on iOS.
2. The laptop's local IP address is not fixed — it depends on whichever device is providing the hotspot that day, and can change between sessions.

The resolution: a **real, publicly-trusted certificate** (so no phone needs to install anything), for a **cheap, disposable domain** (so cost is irrelevant), with the domain's DNS record kept current by a **manual operator step**, never by code the server runs itself.

## Locked constraint this design must respect

The project's acceptance criteria require: **no runtime ACME/DNS-provider calls, no tunnels** — the Convene server process itself must never call Let's Encrypt or Cloudflare's API as part of its own operation, startup included. Certificate issuance and DNS updates are both **operator actions**, run from a separate script or runbook, before the server is started. The server only ever *reads* a certificate file and a configured hostname, and *checks* whether that hostname currently resolves correctly — it never fixes anything itself. This mirrors how certificate renewal was already scoped: "an operator step in the runbook," not server automation.

## End-to-end flow

```text
ONE-TIME SETUP (internet required, done by the developer alone, days before the event):
    buy a cheap domain, DNS hosted on Cloudflare
            |
    issue a Let's Encrypt certificate for that domain via DNS-01 challenge
    (no public-facing server required for this step — see domain-setup.md)
            |
    cert + key saved to the laptop's disk, outside the repo
    (valid ~90 days — one issuance comfortably covers the event)


EVENT DAY, BEFORE STARTING THE SERVER (operator preflight step, manual, internet required):
    hotspot goes up (developer's Android or iPhone, or MacBook Internet Sharing)
            |
    laptop joins the hotspot as a client, gets a local IP (e.g. 172.20.10.4)
            |
    OPERATOR runs the DNS preflight script by hand:
        detects the laptop's current local IP
        updates the Cloudflare A record to point at it
        (this is a standalone script — NOT called by the Convene server)
            |
    operator confirms the update (script prints/confirms success)


THEN, STARTING THE SERVER:
    Convene server starts with --public-host set
            |
    startup CHECKS (read-only, no network calls to any provider):
        does the certificate cover this hostname?
        is the certificate expiring soon?
        does the hostname currently resolve, from the laptop itself,
        to the laptop's own local IP? (catches "operator forgot the
        preflight step" or "DNS hasn't propagated yet")
            |
    if checks pass: laptop serves HTTPS using the already-issued certificate
            |
    operator shares hotspot credentials (or an operator-provided Wi-Fi QR), then the
    dashboard displays the meeting QR: https://<domain>:8443/join/<meeting-id>
            |
    judge/participant joins hotspot → scans the meeting QR → phone resolves the domain via a small DNS
    lookup (works fine even on weak connectivity) → gets the laptop's IP →
    connects DIRECTLY to the laptop over the local hotspot network,
    never touching the internet again for anything else
            |
    HTTPS handshake succeeds (cert matches the domain, publicly trusted,
    no warning, nothing to install) → page loads → mic permission → streaming
```

## Why a real domain instead of a local CA

A Let's Encrypt certificate is trusted by every phone's OS out of the box, because Let's Encrypt is a publicly-trusted Certificate Authority. A `mkcert`-issued certificate is trusted only by machines that have explicitly installed and trusted its local CA — which every participant phone would otherwise need to do manually, with iOS requiring an especially unfriendly profile-install-and-enable-trust flow. This was the actual UX failure the earlier approach ran into, and it's why this document exists.

## Why a real domain doesn't need to be expensive

Let's Encrypt's DNS-01 challenge only verifies that you control DNS for the domain (via a TXT record) — it does not check price, TLD, or "how real" the name looks. A $2/year gibberish domain gets an identical trust level to any other domain.

## Why the laptop's IP can move and this still works

The domain's DNS record is kept current by the operator preflight step (above), run whenever the hotspot session starts or the hotspot device changes. The QR code encoding the join URL never needs to change, because it only ever encodes the domain name — never a raw IP — and only the DNS record's *meaning* (which IP it resolves to) needs to be kept current underneath it.

## Why a raw IP in the QR was rejected

Putting `https://172.20.10.4/join/...` directly in the QR was considered and rejected: the certificate is issued for the domain name, not an IP address. A browser connecting to a bare IP would see a hostname mismatch and throw the exact untrusted-certificate warning this design exists to avoid.

## DNS preflight step (operator-run, not server code)

Run once per hotspot session, before starting the Convene server:

1. Detect the laptop's current local IP on its active network interface (the hotspot it has joined).
2. Update the Cloudflare A record (`PATCH /zones/{zone_id}/dns_records/{record_id}`, Bearer token auth) to that IP, with `proxied: false` explicitly set every time — a structural safeguard against the record ever silently ending up "Proxied" (see below).
3. Confirm the update succeeded (the script's own output) before starting the server.

This lives as a standalone script (proposed path: `scripts/check_https.py` or similar — Claude Code's call, per the task file's existing convention for this area), invoked manually by the operator. It is explicitly **not** imported into or called by any server startup path — that would violate the locked "no runtime DNS-provider calls" constraint.

## Server startup checks (read-only)

At startup, with `--public-host` set, the server:

1. Loads the configured certificate and confirms it covers the configured hostname.
2. Checks the certificate's expiry and warns if it's approaching (non-fatal, informational).
3. Resolves the configured hostname itself (a local DNS lookup, not a provider API call) and compares it against its own detected local IP — if they don't match, this is a strong signal the operator preflight step was skipped or is stale, and should be surfaced as a clear, actionable warning before QR codes are shown.

None of these three checks make a call to Let's Encrypt or Cloudflare — they only read the local certificate file and perform an ordinary DNS resolution, the same kind any browser does.

## Critical Cloudflare configuration requirement: DNS-only, not proxied

The A record **must** be set to "DNS only" (grey cloud in the Cloudflare dashboard), never "Proxied" (orange cloud). Proxied mode routes traffic through Cloudflare's edge network, which cannot reach a private local IP — it doesn't exist on the public internet. A proxied record here silently breaks the entire flow: phones would reach a Cloudflare error page instead of the laptop, with no obvious indication of why. The preflight script forces `proxied: false` on every update as a safeguard against this ever regressing, in addition to the one-time dashboard setting in `domain-setup.md`.

## Wi-Fi access and meeting QR

| Item | Encodes | Purpose |
|---|---|---|
| Hotspot credentials | Operator-provided password or optional Wi-Fi QR | Phones join the hotspot before opening Convene. A dashboard-generated Wi-Fi QR is not implemented by CON-04B. |
| Meeting QR | `https://<domain>:8443/join/<meeting_id>` | Generated fresh per meeting (`meeting_id` changes every time — see `api.md`'s `POST /api/meetings`), domain never changes |

The dashboard displays the meeting QR. Any Wi-Fi QR is supplied outside Convene until explicitly added as a separate UI feature.

## Hotspot device options and tradeoffs

| Device | Consideration |
|---|---|
| Android phone hotspot | Largest typical client subnet (~250 addresses) — safest choice if expecting many simultaneous phones |
| iPhone Personal Hotspot | Small subnet (~13 usable addresses) — could become a ceiling with 10+ participant phones plus judges; test at actual expected headcount before relying on this |
| MacBook Internet Sharing | Needs the MacBook to have its own upstream connection to share (venue Wi-Fi, tethered phone, ethernet) — one additional link in the chain, but avoids needing a second physical hotspot device |

Whichever is used, verify before the event:
- **Client isolation is off** — connected devices must be able to reach each other (specifically, reach the laptop), not just "the internet." Some hotspot implementations block this by default.
- **DNS-over-private-IP is not filtered** — some resolvers refuse to return a private/RFC1918 address for security reasons (anti–DNS-rebinding). Test with the actual phones/carriers expected at the event, don't assume.

## Connectivity requirement, stated honestly

This design needs **some** internet at the hotspot — even weak connectivity is sufficient, since the only thing that crosses it is a small DNS lookup (roughly a hundred bytes) per phone, once, at join time, plus the operator's own preflight-script run. It is not a zero-internet design; that tradeoff was made explicitly (`decisions.md` ADR-20) in exchange for dropping the travel router and its install-free-but-hardware-dependent alternative. If the venue turns out to have genuinely zero connectivity at any point, this design does not handle that case — there is no documented fallback for it, since the router-based alternative has been dropped, not parked.

## Failure handling

| Failure | Handling |
|---|---|
| Operator forgets to run the preflight script, or hotspot IP changed after it ran | Server startup's resolution check (above) catches the mismatch and warns clearly before showing QR codes |
| Preflight script fails (no connectivity yet at the hotspot) | Script reports failure plainly; operator retries once connectivity is available; server is not started until it succeeds |
| DNS record still shows the old IP briefly after a successful update (propagation lag) | A short TTL is set on the record (`domain-setup.md`) to minimize this; the server's own resolution check will still catch a stale result and warn rather than silently proceeding |
| A record accidentally left "Proxied" | Preflight script forces `proxied: false` on every run as a structural safeguard, not just a one-time dashboard setting |
| Hotspot hits its device-count ceiling | Not automatically recoverable — check the device options table above against expected headcount ahead of time, not discovered live |
| Certificate expired or doesn't cover the configured hostname | Fatal startup check — server refuses to start rather than serving a broken HTTPS connection |

## Dropped alternatives (locked, do not resurrect without revisiting ADR-20)

- **`mkcert` + local CA** (original `old_docs/HTTPS_LOCAL.md`): required every phone to manually install and trust a custom CA — an unacceptable UX for a "scan and go" demo, especially on iOS.
- **Travel router + local DNS override**: would have provided genuinely zero-internet operation, at the cost of buying and configuring dedicated hardware. Explicitly dropped, not kept as a fallback — hotspot-only is now the sole supported path.

## Relationship to other documents

`domain-setup.md` covers the one-time domain purchase, Cloudflare configuration, and certificate issuance steps this document depends on. `deployment.md`'s network setup section reflects this flow. `decisions.md` ADR-20 records why this approach was chosen, including the "no runtime provider calls" constraint and the decision to drop the travel router.
