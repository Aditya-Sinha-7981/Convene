"""The phone join page (client/app.js) driven by a fake browser under node.

Covers the page's protocol and retry logic: persisted device_id, registration, join/offer/answer, reconnect with
a fresh peer, backoff, the retry button, stop, and the error paths. It is not a browser: real WebRTC, microphone
permission, certificate trust and Wi-Fi behavior need real phones (logs/transport.md, R1 to R9). Skipped when
node is not installed.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

HARNESS = Path(__file__).parent / "js" / "join_page_harness.mjs"
NODE = shutil.which("node")


def run_node(*args):
    return subprocess.run([NODE, str(HARNESS), *args], capture_output=True, text=True, timeout=60)


def scenario_names():
    if NODE is None:
        return []
    listed = run_node("--list")
    assert listed.returncode == 0, listed.stderr
    return listed.stdout.strip().splitlines()


SCENARIOS = scenario_names()


@pytest.mark.skipif(NODE is None, reason="node is not installed")
def test_the_harness_found_the_scenarios():
    assert len(SCENARIOS) >= 15


@pytest.mark.skipif(NODE is None, reason="node is not installed")
@pytest.mark.parametrize("scenario", SCENARIOS)
def test_join_page(scenario):
    result = run_node(scenario)
    assert result.returncode == 0, f"{result.stdout}\n{result.stderr}"
    assert f"ok: {scenario}" in result.stdout
