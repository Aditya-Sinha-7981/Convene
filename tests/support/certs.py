"""Generate throwaway TLS certificates for tests (never mkcert, never trusted by anything)."""
import datetime
import ipaddress
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID


def make_certificate(directory: Path, *, ips=(), dns=(), days: int = 30, name: str = "cert") -> tuple[Path, Path]:
    """Write ``<name>.pem`` and ``<name>-key.pem``. ``days`` may be negative for an already-expired certificate."""
    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "convene-test")])
    now = datetime.datetime.now(datetime.timezone.utc)
    names = [x509.IPAddress(ipaddress.ip_address(ip)) for ip in ips] + [x509.DNSName(d) for d in dns]
    builder = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
               .serial_number(x509.random_serial_number())
               .not_valid_before(now - datetime.timedelta(days=max(-days, 0) + 2))
               .not_valid_after(now + datetime.timedelta(days=days)))
    if names:
        builder = builder.add_extension(x509.SubjectAlternativeName(names), critical=False)
    cert = builder.sign(key, hashes.SHA256())
    cert_path, key_path = directory / f"{name}.pem", directory / f"{name}-key.pem"
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                           serialization.NoEncryption()))
    return cert_path, key_path
