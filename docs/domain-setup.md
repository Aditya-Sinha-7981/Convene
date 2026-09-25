# Domain Setup (One-Time Runbook)

This is a step-by-step runbook, not an architecture document — see `network-and-https.md` for the reasoning behind each step. Do all of this once, days before the event, on a normal internet connection. None of it needs to happen at the venue.

## 1. Buy a cheap domain

Any registrar works; Cloudflare Registrar is convenient because it keeps registration and DNS management in one place, at cost price (no markup). Any TLD is fine — cost and "realness" don't matter (`network-and-https.md`). A short, typo-resistant string still helps since judges will occasionally glance at the URL bar.

## 2. Point the domain at Cloudflare DNS

If not already registered through Cloudflare, add the domain to a Cloudflare account and update the registrar's nameservers to Cloudflare's — needed regardless of registrar, since the DNS-01 challenge and the dynamic update script both go through Cloudflare's API.

## 3. Create a scoped API token

Cloudflare dashboard → My Profile → API Tokens → Create Token. Use a scoped custom token, not the legacy Global API Key:

- Permission: `Zone` → `DNS` → `Edit`
- Zone Resources: restrict to this specific domain only
- Save the token securely — it goes in the laptop's `.env` file, never committed to source control (see `.env.example` alongside the code in this task)

## 4. Note the Zone ID

Cloudflare dashboard → the domain's overview page → "Zone ID" is shown in the right-hand sidebar. This, plus the API token, is everything `scripts/update_dns.py` needs.

## 5. Create the A record (placeholder, will be updated dynamically later)

Add an A record:

- Type: `A`
- Name: `@` (or a subdomain, e.g. `join`, if preferred — pick one and use it consistently everywhere else in this doc set and the code)
- Content: any placeholder IP for now (e.g. `192.0.2.1` — reserved for documentation use, will never actually be used)
- **Proxy status: DNS only (grey cloud) — not Proxied.** This is the single most important setting in this whole runbook; a proxied record silently breaks everything (`network-and-https.md`).
- TTL: as low as Cloudflare allows (`Auto` is typically fine, or explicitly `1 min`/`2 min` if offered) — minimizes propagation lag between the dynamic update at server startup and phones being able to resolve it correctly.

## 6. Issue the certificate via DNS-01 (no public server required)

Using `acme.sh` (or `certbot` with a Cloudflare DNS plugin — `acme.sh` shown here since it has first-class Cloudflare support with no plugin install):

```bash
# one-time install
curl https://get.acme.sh | sh -s email=your-email@example.com

# export Cloudflare credentials for the DNS-01 challenge
export CF_Token="<the API token from step 3>"
export CF_Zone_ID="<the zone ID from step 4>"

# issue the certificate
~/.acme.sh/acme.sh --issue --dns dns_cf -d convene-x7q.xyz

# install it to a stable, known location the server will read from
~/.acme.sh/acme.sh --install-cert -d convene-x7q.xyz \
  --key-file       /path/to/convene/certs/privkey.pem \
  --fullchain-file /path/to/convene/certs/fullchain.pem
```

This does not require the laptop (or any server) to be reachable from the public internet — `acme.sh` only needs to create/verify a DNS TXT record via the Cloudflare API, which works from any machine with internet access.

## 7. Verify the certificate files

```bash
openssl x509 -in /path/to/convene/certs/fullchain.pem -noout -dates -subject
```

Confirm the `subject` matches the domain and `notAfter` is roughly 90 days out (see `network-and-https.md`'s validity note — Let's Encrypt's default validity is 90 days as of this writing).

## 8. Fill in the laptop's `.env`

```bash
CONVENE_DOMAIN=convene-x7q.xyz
CLOUDFLARE_API_TOKEN=<the token from step 3>
CLOUDFLARE_ZONE_ID=<the zone ID from step 4>
CLOUDFLARE_DNS_RECORD_NAME=convene-x7q.xyz   # or join.convene-x7q.xyz, matching step 5
TLS_CERT_PATH=/path/to/convene/certs/fullchain.pem
TLS_KEY_PATH=/path/to/convene/certs/privkey.pem
```

`deployment.md` and `scripts/update_dns.py` both read from this file — see `.env.example` in the code.

## 9. Sanity-check before the event

From a phone on a normal cellular connection (not the hotspot):

```bash
curl -I https://convene-x7q.xyz
```

Confirm no certificate warning and a valid HTTP response. This confirms the certificate itself is correctly installed and served — it does not yet confirm the dynamic DNS update or hotspot flow, which should be tested separately per `network-and-https.md`'s failure-handling table, ideally at the actual venue or a close approximation of it.

## Renewal

Not required for a single event given the 90-day validity — but if this domain/setup gets reused for a later event more than ~75 days out, re-run step 6 (`acme.sh --renew` is simpler once the initial issuance has been done). Do this again on a normal internet connection, never dependent on venue connectivity.

## What this runbook deliberately does not cover

Setting up the actual Convene application, the hotspot itself, or the QR code display — all covered in `network-and-https.md` and `deployment.md`. This document is scoped strictly to "how does the domain/certificate exist and get onto the laptop's disk," which only needs to happen once.
