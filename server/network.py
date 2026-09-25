"""LAN address detection, join URL and QR generation, and the startup certificate check.

Nothing here touches the internet: address detection reads local interfaces, and the UDP "route probe"
sends no packets. (docs/transport.md "Network addressing", docs/deployment.md "Startup sequence".)
"""
import io
import ipaddress
import re
import socket
import ssl
import threading
from datetime import datetime, timezone
from pathlib import Path

import qrcode
from cryptography import x509
from qrcode.image.svg import SvgPathImage


class CertificateError(Exception):
    """The TLS certificate cannot be used for the advertised address. Fails startup with an actionable message."""


class HostnameError(ValueError):
    """A configured public hostname cannot safely be used in a participant URL."""


class ResolutionError(Exception):
    """The operator-facing hostname resolution check could not complete."""


_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")


def validate_public_host(host: str) -> str:
    """Return a strict lowercase DNS hostname, rejecting URLs and IP literals.

    A hostname is deliberately narrower than every DNS name browsers might accept: this avoids surprising QR
    contents and keeps the public-certificate runbook to one portable, public DNS name.
    """
    if not isinstance(host, str) or not host:
        raise HostnameError("--public-host must be a non-empty lowercase DNS hostname")
    if host != host.lower() or host.endswith(".") or ":" in host or "/" in host or any(char.isspace() for char in host):
        raise HostnameError(f"--public-host must be a lowercase DNS hostname, got {host!r}")
    try:
        ipaddress.ip_address(host)
    except ValueError:
        pass
    else:
        raise HostnameError(f"--public-host must be a hostname, not IP {host}; use --advertise-ip for an IP")
    labels = host.split(".")
    if len(host) > 253 or any(not _DNS_LABEL.fullmatch(label) for label in labels):
        raise HostnameError(f"--public-host must be a valid lowercase DNS hostname, got {host!r}")
    return host


def _is_private_ipv4(address: str) -> bool:
    try:
        ip = ipaddress.ip_address(address)
    except ValueError:
        return False
    return ip.version == 4 and ip.is_private and not ip.is_loopback and not ip.is_link_local


def local_addresses() -> list[str]:
    """Private IPv4 addresses of this machine, the default-route interface first.

    The route probe connects a UDP socket to a documentation address (RFC 5737) without sending anything;
    the OS then reports which local address it would use. It works with no internet as long as the machine
    has a route, for example on a phone hotspot. The hostname lookup is the fallback.
    """
    found: list[str] = []
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        for result in socket.getaddrinfo(socket.gethostname(), None, family=socket.AF_INET):
            found.append(result[4][0])
    except OSError:
        pass
    unique: list[str] = []
    for address in found:
        if _is_private_ipv4(address) and address not in unique:
            unique.append(address)
    return unique


def detect_lan_address(advertise_ip: str | None = None) -> str | None:
    """The address to put in join URLs: ``advertise_ip`` if given (validated), else the first detected one."""
    if advertise_ip:
        return str(ipaddress.IPv4Address(advertise_ip))
    detected = local_addresses()
    return detected[0] if detected else None


def join_url(host: str, port: int, meeting_id: str) -> str:
    return f"https://{host}:{port}/join/{meeting_id}"


def qr_svg(url: str) -> str:
    """An inline SVG document encoding ``url`` and nothing else (works with no network)."""
    buffer = io.BytesIO()
    qrcode.make(url, image_factory=SvgPathImage).save(buffer)
    return buffer.getvalue().decode("utf-8")


def certificate_names(cert_path: str | Path) -> tuple[list[str], list[str], datetime]:
    """(DNS names, IP addresses, expiry) from the certificate's subject alternative names."""
    try:
        cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    except (OSError, ValueError) as exc:
        raise CertificateError(f"cannot read certificate {cert_path}: {exc}") from exc
    try:
        san = cert.extensions.get_extension_for_class(x509.SubjectAlternativeName).value
        dns = san.get_values_for_type(x509.DNSName)
        ips = [str(ip) for ip in san.get_values_for_type(x509.IPAddress)]
    except x509.ExtensionNotFound:
        dns, ips = [], []
    return dns, ips, cert.not_valid_after_utc


def _certificate_looks_private(cert_path: str | Path) -> bool:
    """Best-effort warning only: public trust is established by the phone, not this heuristic."""
    cert = x509.load_pem_x509_certificate(Path(cert_path).read_bytes())
    issuer = cert.issuer.rfc4514_string().lower()
    return cert.subject == cert.issuer or "mkcert" in issuer


def _dns_name_matches(host: str, certificate_name: str) -> bool:
    host, certificate_name = host.lower(), certificate_name.lower()
    if certificate_name.startswith("*."):
        suffix = certificate_name[2:]
        # A wildcard covers exactly one label: ``*.demo.example`` covers ``phone.demo.example`` only.
        return host.endswith("." + suffix) and host.count(".") == suffix.count(".") + 1
    return host == certificate_name


def resolve_hostname(host: str, *, timeout_s: float = 2.0) -> list[str]:
    """Resolve through the OS resolver without allowing a stalled lookup to hold startup indefinitely."""
    result: list[object] = []

    def lookup() -> None:
        try:
            result.append(socket.getaddrinfo(host, None, type=socket.SOCK_STREAM))
        except OSError as exc:
            result.append(exc)

    thread = threading.Thread(target=lookup, daemon=True)
    thread.start()
    thread.join(timeout_s)
    if thread.is_alive():
        raise ResolutionError(f"timed out after {timeout_s:g} s")
    if not result:
        raise ResolutionError("resolver returned no result")
    if isinstance(result[0], OSError):
        raise ResolutionError(str(result[0])) from result[0]
    addresses: list[str] = []
    for entry in result[0]:
        address = entry[4][0]
        if address not in addresses:
            addresses.append(address)
    if not addresses:
        raise ResolutionError("resolver returned no addresses")
    return addresses


def resolution_warning(host: str, lan_address: str | None, *, timeout_s: float = 2.0) -> str | None:
    """Return a non-fatal operator diagnostic for public-host DNS; never contacts a DNS provider API."""
    if lan_address is None:
        return f"WARNING: cannot compare DNS for {host}: no LAN address was detected"
    try:
        addresses = resolve_hostname(host, timeout_s=timeout_s)
    except ResolutionError as exc:
        return (f"WARNING: {host} did not resolve within the startup check ({exc}). Run scripts/update_dns.py "
                f"on the hotspot, then confirm it maps to {lan_address}.")
    if lan_address not in addresses:
        return (f"WARNING: {host} resolves to {', '.join(addresses)}, not this laptop's LAN address {lan_address}. "
                "Run scripts/update_dns.py and wait for DNS to update before participants scan the QR.")
    return None


def check_certificate(cert_path: str | Path, key_path: str | Path, address: str | None, *,
                      now: datetime | None = None) -> list[str]:
    """Fail loudly unless the certificate loads with its key, is unexpired, and covers ``address``.

    Phones reject a certificate whose names do not include the exact address in the join URL, and a changed
    hotspot IP silently breaks them, so this is checked before the server starts.
    """
    try:
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        context.load_cert_chain(str(cert_path), str(key_path))
    except (OSError, ssl.SSLError) as exc:
        raise CertificateError(f"certificate {cert_path} and key {key_path} do not load together: {exc}") from exc
    dns, ips, expires = certificate_names(cert_path)
    now = now or datetime.now(timezone.utc)
    if expires <= now:
        raise CertificateError(f"certificate {cert_path} expired on {expires:%Y-%m-%d}; generate a new one")
    warnings: list[str] = []
    remaining = expires - now
    if remaining.days < 14:
        warnings.append(f"WARNING: certificate expires on {expires:%Y-%m-%d} ({remaining.days + 1} days remaining); renew before the demo")
    if address is None:
        return warnings
    try:
        target = ipaddress.ip_address(address)
    except ValueError:  # a hostname, matched against the DNS names
        if any(_dns_name_matches(address, name) for name in dns):
            if _certificate_looks_private(cert_path):
                warnings.append("WARNING: hostname mode is using a self-signed or private-CA-looking certificate; "
                                "phones without that CA will reject it")
            return warnings
    else:
        if any(ipaddress.ip_address(ip) == target for ip in ips):
            return warnings
    covered = ", ".join([*ips, *dns]) or "no names at all"
    if not _is_private_ipv4(address):
        remedy = "Re-issue the public certificate for that hostname and serve its full chain"
    else:
        remedy = f"Generate one that does, for example: mkcert -cert-file {cert_path} -key-file {key_path} {address}"
    raise CertificateError(
        f"certificate {cert_path} does not cover {address}; it covers: {covered}. Phones will refuse it. "
        + remedy)
