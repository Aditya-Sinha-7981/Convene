# DT-17 Local HTTPS and Mobile Microphone Access

## Why this document exists

Mobile browsers normally require `getUserMedia()` to run in a secure context.

The obvious local URL:

```text
http://192.168.x.x:PORT
```

should therefore NOT be assumed to allow microphone access.

This is one of the first things to validate.

## Development approach

Use HTTPS for the laptop-hosted site.

A practical development option is `mkcert`, which creates a locally trusted development CA and certificates.

The broad process is:

```text
Install mkcert
      |
create local CA
      |
create certificate for laptop local hostname/IP
      |
serve HTTPS
      |
trust CA on test phones
      |
open HTTPS URL
      |
allow microphone
```

Exact trust steps vary by Android/iOS version and browser.

## Important

Do not build the prototype around:

- Chrome flags that disable security
- Safari experimental settings
- browser command-line flags
- public Internet tunneling
- ngrok/cloud tunnels
- Tailscale/WireGuard as a required dependency

Those can be useful for development, but they invalidate the experiment's "works on an isolated local network" requirement.

## Certificate naming

The certificate must cover the hostname/address actually used by the phone.

If the join URL is:

```text
https://192.168.50.10:8443/join/TEST
```

the certificate must be valid for the way the browser connects.

If using a local hostname such as:

```text
https://dt17.local:8443
```

make sure the phones can resolve that hostname on the local network and the certificate covers it.

Do not assume `.local` discovery and certificate validation will automatically behave identically across Android and iOS.

## Development fallback

If a target phone refuses to trust the development CA, document the exact behavior.

Do not silently switch to insecure browser flags.

The purpose of this test is to discover real browser constraints before the hackathon.

## Production/hackathon goal

The eventual hackathon setup should ideally make HTTPS transparent to participants:

```text
Scan
  |
HTTPS page
  |
Allow microphone
  |
Done
```

Participants should not be asked to install certificates or change browser security settings.

If the development certificate workflow is too cumbersome for real users, the team must solve certificate trust separately before the event.

## Security priority

Security is intentionally low priority for this prototype.

However, HTTPS is not being introduced for confidentiality.

It is primarily required because browser microphone APIs enforce secure-context restrictions.

The network can remain completely isolated from the Internet.
