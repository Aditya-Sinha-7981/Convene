"""LAN address detection, join URL and QR generation, and the startup certificate check.

Nothing here touches the internet: address detection reads local interfaces, and the UDP "route probe"
sends no packets. (docs/transport.md "Network addressing", docs/deployment.md "Startup sequence".)
"""
import io
import ipaddress
import socket
import ssl
from datetime import datetime, timezone
from pathlib import Path

import qrcode
from cryptography import x509
from qrcode.image.svg import SvgPathImage


class CertificateError(Exception):
    """The TLS certificate cannot be used for the advertised address. Fails startup with an actionable message."""


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


def check_certificate(cert_path: str | Path, key_path: str | Path, address: str | None, *,
                      now: datetime | None = None) -> None:
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
    if expires < now:
        raise CertificateError(f"certificate {cert_path} expired on {expires:%Y-%m-%d}; generate a new one")
    if address is None:
        return
    try:
        target = ipaddress.ip_address(address)
    except ValueError:  # a hostname, matched against the DNS names
        if address.lower() in (name.lower() for name in dns):
            return
    else:
        if any(ipaddress.ip_address(ip) == target for ip in ips):
            return
    covered = ", ".join([*ips, *dns]) or "no names at all"
    raise CertificateError(
        f"certificate {cert_path} does not cover {address}; it covers: {covered}. Phones will refuse it. "
        f"Generate one that does, for example: mkcert -cert-file {cert_path} -key-file {key_path} {address}")
