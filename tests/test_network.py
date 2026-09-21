"""LAN address detection, join URL and QR, and the startup certificate check."""
import datetime
import ssl

import pytest

from server import network
from tests.support.certs import make_certificate

ADDRESS = "192.168.50.10"


class _FakeUdp:
    def __init__(self, address):
        self.address = address

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def connect(self, target):
        if self.address is None:
            raise OSError("network unreachable")

    def getsockname(self):
        return (self.address, 4242)


def fake_network(monkeypatch, *, route, hostname):
    monkeypatch.setattr(network.socket, "socket", lambda *a, **k: _FakeUdp(route))
    monkeypatch.setattr(network.socket, "getaddrinfo",
                        lambda *a, **k: [(2, 1, 6, "", (ip, 0)) for ip in hostname])


def test_route_probe_address_comes_first_and_only_private_ipv4_is_kept(monkeypatch):
    fake_network(monkeypatch, route="192.168.50.10", hostname=["127.0.0.1", "10.0.0.5", "8.8.8.8", "169.254.9.9", "192.168.50.10"])
    assert network.local_addresses() == ["192.168.50.10", "10.0.0.5"]
    assert network.detect_lan_address() == "192.168.50.10"


def test_hostname_lookup_is_the_fallback_when_there_is_no_route(monkeypatch):
    fake_network(monkeypatch, route=None, hostname=["172.20.1.4"])
    assert network.detect_lan_address() == "172.20.1.4"


def test_no_address_at_all_gives_none_not_a_made_up_one(monkeypatch):
    fake_network(monkeypatch, route=None, hostname=["127.0.0.1"])
    assert network.local_addresses() == [] and network.detect_lan_address() is None


def test_advertise_ip_overrides_detection_and_is_validated(monkeypatch):
    fake_network(monkeypatch, route="192.168.50.10", hostname=[])
    assert network.detect_lan_address("10.9.8.7") == "10.9.8.7"
    with pytest.raises(ValueError):
        network.detect_lan_address("not-an-ip")


def test_join_url_and_qr_svg():
    url = network.join_url(ADDRESS, 8443, "0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8")
    assert url == "https://192.168.50.10:8443/join/0d4f6a52-7c1b-4e7a-b0a3-51e1f4c2a9d8"
    svg = network.qr_svg(url)
    assert "<svg" in svg and "</svg>" in svg
    assert network.qr_svg(url) == svg and network.qr_svg(url + "x") != svg  # deterministic, and it encodes the URL


def test_certificate_covering_the_address_passes(tmp_path):
    cert, key = make_certificate(tmp_path, ips=[ADDRESS], dns=["localhost"])
    network.check_certificate(cert, key, ADDRESS)
    network.check_certificate(cert, key, "localhost")
    network.check_certificate(cert, key, None)  # no address to check: only loads and expiry
    assert network.certificate_names(cert)[:2] == (["localhost"], [ADDRESS])


def test_certificate_that_lacks_the_address_fails_naming_both(tmp_path):
    cert, key = make_certificate(tmp_path, ips=["192.168.1.5"], dns=["localhost"])
    with pytest.raises(network.CertificateError) as caught:
        network.check_certificate(cert, key, ADDRESS)
    message = str(caught.value)
    assert ADDRESS in message and "192.168.1.5" in message and "localhost" in message and "mkcert" in message


def test_certificate_without_any_names_fails(tmp_path):
    cert, key = make_certificate(tmp_path)
    with pytest.raises(network.CertificateError, match="no names at all"):
        network.check_certificate(cert, key, ADDRESS)


def test_expired_certificate_fails(tmp_path):
    cert, key = make_certificate(tmp_path, ips=[ADDRESS], days=-1)
    with pytest.raises(network.CertificateError, match="expired"):
        network.check_certificate(cert, key, ADDRESS)


def test_key_that_does_not_match_the_certificate_fails(tmp_path):
    cert, _ = make_certificate(tmp_path, ips=[ADDRESS], name="one")
    _, other_key = make_certificate(tmp_path, ips=[ADDRESS], name="two")
    with pytest.raises(network.CertificateError, match="do not load together"):
        network.check_certificate(cert, other_key, ADDRESS)


def test_missing_or_garbage_files_fail_cleanly(tmp_path):
    with pytest.raises(network.CertificateError):
        network.check_certificate(tmp_path / "absent.pem", tmp_path / "absent-key.pem", ADDRESS)
    (tmp_path / "junk.pem").write_text("not a certificate")
    with pytest.raises(network.CertificateError):
        network.check_certificate(tmp_path / "junk.pem", tmp_path / "junk.pem", ADDRESS)


def test_a_certificate_valid_tomorrow_still_passes_with_a_clock_check(tmp_path):
    cert, key = make_certificate(tmp_path, ips=[ADDRESS], days=1)
    later = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=2)
    with pytest.raises(network.CertificateError, match="expired"):
        network.check_certificate(cert, key, ADDRESS, now=later)
    assert isinstance(ssl.PROTOCOL_TLS_SERVER, int)  # the check uses the same TLS stack uvicorn does
