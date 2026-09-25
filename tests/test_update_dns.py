"""The Cloudflare preflight is explicit operator tooling; tests never make a real provider call."""
import importlib.util
from pathlib import Path

import pytest


SPEC = importlib.util.spec_from_file_location("update_dns", Path(__file__).parents[1] / "scripts" / "update_dns.py")
update_dns = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(update_dns)


def env_file(tmp_path, extra=""):
    path = tmp_path / ".env"
    path.write_text("\n".join((
        "CONVENE_DOMAIN=convene.example.com",
        "CLOUDFLARE_API_TOKEN=secret-not-for-output",
        "CLOUDFLARE_ZONE_ID=zone-id",
        "CLOUDFLARE_DNS_RECORD_NAME=convene.example.com",
        extra,
    )))
    return path


def test_load_env_never_executes_shell_and_requires_the_single_host_shape(tmp_path):
    values = update_dns.load_env(env_file(tmp_path))
    assert values["CONVENE_DOMAIN"] == "convene.example.com"
    bad = env_file(tmp_path, "CLOUDFLARE_DNS_RECORD_NAME=other.example.com")
    with pytest.raises(update_dns.PreflightError, match="must equal"):
        update_dns.load_env(bad)


def test_update_existing_record_forces_dns_only_without_leaking_token(monkeypatch, tmp_path):
    calls = []

    def fake_request(method, path, token, **kwargs):
        calls.append((method, path, token, kwargs.get("payload")))
        if method == "GET":
            return {"success": True, "result": [{"id": "record-id"}]}
        return {"success": True, "result": {}}

    monkeypatch.setattr(update_dns, "request_json", fake_request)
    assert update_dns.update_record(update_dns.load_env(env_file(tmp_path)), "192.168.50.10") == "updated"
    assert calls[1][0] == "PUT" and calls[1][3] == {"type": "A", "name": "convene.example.com",
                                                        "content": "192.168.50.10", "ttl": 60, "proxied": False}


def test_update_can_create_and_dry_run_without_a_mutation(monkeypatch, tmp_path):
    calls = []
    monkeypatch.setattr(update_dns, "request_json", lambda *args, **kwargs: calls.append((args, kwargs)) or {"result": []})
    values = update_dns.load_env(env_file(tmp_path))
    assert update_dns.update_record(values, "192.168.50.10", dry_run=True) == "would create"
    assert len(calls) == 1  # list only; no POST
    assert update_dns.update_record(values, "192.168.50.10") == "created"
    assert calls[-1][0][0] == "POST"
