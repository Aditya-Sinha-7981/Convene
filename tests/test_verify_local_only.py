import importlib.util
from pathlib import Path


SPEC = importlib.util.spec_from_file_location("verify_local_only", Path(__file__).parents[1] / "scripts" / "verify_local_only.py")
verify = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(verify)


HEADER = "COMMAND PID USER FD TYPE DEVICE SIZE/OFF NODE NAME\n"


def rows(*names):
    return HEADER + "\n".join(f"Python 123 demo {index}u IPv4 0t0 TCP {name} (ESTABLISHED)" for index, name in enumerate(names))


def test_lsof_parser_accepts_loopback_and_private_lan_peers():
    output = rows("127.0.0.1:8443->127.0.0.1:60123", "192.168.1.5:8443->192.168.1.44:51001",
                  "[fd00::1]:8443->[fd00::2]:51002")
    assert verify.non_local_peers(output) == []


def test_lsof_parser_rejects_public_and_hostname_peers():
    output = rows("192.168.1.5:8443->8.8.8.8:443", "192.168.1.5:8443->api.example.com:443")
    assert verify.non_local_peers(output) == ["8.8.8.8", "api.example.com"]


def test_listener_rows_are_not_treated_as_remote_connections():
    output = HEADER + "Python 123 demo 8u IPv4 0t0 TCP *:8443 (LISTEN)\n"
    assert verify.non_local_peers(output) == []
