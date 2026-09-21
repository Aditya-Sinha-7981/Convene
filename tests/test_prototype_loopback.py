"""Synthetic-phone loopback through the prototype's real receive path (CON-01).

A synthetic phone is an in-process aiortc client. These tests show the server's
receive/cleanup code works with a WebRTC peer; they do NOT establish phone, browser,
Wi-Fi, HTTPS, screen-lock or latency behavior, which need real devices (see logs/transport.md).
"""
import asyncio
import logging
import re
import time

import pytest

from server import app as prototype
from tests.support.synthetic_phone import SyntheticPhone

KEY = ("TEST-123", "a" * 32)


async def wait_for(predicate, timeout=10.0, interval=0.05):
    deadline = time.monotonic() + timeout
    while not predicate():
        if time.monotonic() > deadline:
            raise TimeoutError("condition not met")
        await asyncio.sleep(interval)


@pytest.fixture
async def make_phone(signaling_url):
    made = []

    def make(token="a" * 32):
        phone = SyntheticPhone(signaling_url, token)
        made.append(phone)
        return phone

    yield make
    for phone in made:
        await phone.close()


async def metrics_rows(server):
    from aiohttp import ClientSession
    async with ClientSession() as session:
        async with session.get(server.make_url("/metrics")) as response:
            return {row["participant"]: row for row in await response.json()}


async def test_tone_reaches_the_server_receive_path(server, make_phone):
    phone = make_phone()
    answer = await phone.connect()
    assert answer["type"] == "answer" and "m=audio" in answer["sdp"]
    await phone.wait_connected()

    participant = prototype.participants[KEY]
    await wait_for(lambda: participant["samples"] >= 48000)

    assert participant["rate"] == 48000
    assert participant["last_audio"] is not None
    row = (await metrics_rows(server))[phone.participant_id]
    assert row["state"] == "connected"
    assert row["audioReceived"] is True
    assert row["audioDurationSeconds"] >= 1.0
    assert row["lastAudioAgeMs"] is not None and row["lastAudioAgeMs"] < 2000
    assert row["reconnects"] == 0

    before = participant["samples"]
    await wait_for(lambda: participant["samples"] > before + 24000)  # duration keeps rising


async def test_one_second_windows_are_logged_and_receive_age_is_not_latency(make_phone, caplog):
    """The AUDIO log's receive_age_ms is computed from the same frame's receipt time immediately
    after it is set, so it reads ~0 by construction. It is not evidence of transport latency."""
    caplog.set_level(logging.INFO, logger="dt17")
    phone = make_phone()
    await phone.connect()
    await phone.wait_connected()
    participant = prototype.participants[KEY]

    def windows():
        return [r.getMessage() for r in caplog.records if r.getMessage().startswith("AUDIO")]

    await wait_for(lambda: len(windows()) >= 2, timeout=15)
    pattern = re.compile(r"AUDIO \[(p[0-9a-f]{6})\] window=(\d+) duration_ms=(\d+) receive_age_ms=(\d+)")
    parsed = [pattern.match(line) for line in windows()[:2]]
    assert all(parsed), windows()
    assert [int(m.group(2)) for m in parsed] == [1, 2]
    assert all(m.group(1) == phone.participant_id for m in parsed)
    assert all(int(m.group(3)) == 1000 for m in parsed)
    assert all(int(m.group(4)) <= 5 for m in parsed)
    assert participant["pending_stt"] == 0  # STT_COMMAND unset: nothing scheduled


async def test_closing_the_socket_closes_the_peer_and_ends_the_audio_task(server, make_phone):
    """Closing the peer ends the track, so receive_audio stops by itself (\"AUDIO ... stopped\");
    close_peer's cancel() then hits an already-finished task. Either way no receive loop is left running."""
    phone = make_phone()
    await phone.connect()
    await phone.wait_connected()
    participant = prototype.participants[KEY]
    await wait_for(lambda: participant["samples"] > 0)
    peer, task = participant["peer"], participant["audio_task"]
    assert peer is not None and task is not None and not task.done()

    await phone.drop_signaling()
    await wait_for(lambda: participant["peer"] is None and participant["audio_task"] is None)

    assert peer.connectionState == "closed"
    assert task.done()
    assert participant["ws"] is None
    row = (await metrics_rows(server))[phone.participant_id]
    assert row["state"] == "disconnected"
    # Counters survive; the record is not evicted.
    assert row["audioDurationSeconds"] > 0
    frozen = participant["samples"]
    await asyncio.sleep(0.3)
    assert participant["samples"] == frozen


async def test_reconnect_with_same_token_resumes_the_same_participant(server, make_phone):
    first = make_phone()
    await first.connect()
    await first.wait_connected()
    participant = prototype.participants[KEY]
    await wait_for(lambda: participant["samples"] >= 24000)
    old_peer = participant["peer"]

    second = make_phone()  # same token, as the browser does after a drop
    await second.connect()
    await second.wait_connected()

    assert second.participant_id == first.participant_id
    assert second.reconnects == 1
    assert len(prototype.participants) == 1
    assert old_peer.connectionState == "closed"
    assert participant["peer"] is not old_peer
    resumed = participant["samples"]
    await wait_for(lambda: participant["samples"] > resumed + 24000)
    row = (await metrics_rows(server))[second.participant_id]
    assert row["state"] == "connected" and row["reconnects"] == 1


async def test_dropping_one_phone_does_not_stop_another(server, make_phone):
    a, b = make_phone("a" * 32), make_phone("b" * 32)
    for phone in (a, b):
        await phone.connect()
    for phone in (a, b):
        await phone.wait_connected()
    part_a = prototype.participants[("TEST-123", "a" * 32)]
    part_b = prototype.participants[("TEST-123", "b" * 32)]
    await wait_for(lambda: part_a["samples"] > 24000 and part_b["samples"] > 24000)
    assert a.participant_id != b.participant_id

    await b.drop_signaling()
    await wait_for(lambda: part_b["peer"] is None)

    before = part_a["samples"]
    await wait_for(lambda: part_a["samples"] > before + 24000)
    rows = await metrics_rows(server)
    assert rows[a.participant_id]["state"] == "connected"
    assert rows[b.participant_id]["state"] == "disconnected"

    # B returns under its old ID and resumes; A is untouched.
    b2 = make_phone("b" * 32)
    await b2.connect()
    await b2.wait_connected()
    assert b2.participant_id == b.participant_id and b2.reconnects == 1
    assert rows[a.participant_id]["reconnects"] == 0
    assert part_a["reconnects"] == 0


async def test_each_participant_gets_its_own_sample_counter(make_phone):
    """Streams are counted per participant; there is no shared counter between phones."""
    a, b = make_phone("a" * 32), make_phone("b" * 32)
    for phone in (a, b):
        await phone.connect()
    for phone in (a, b):
        await phone.wait_connected()
    part_a = prototype.participants[("TEST-123", "a" * 32)]
    part_b = prototype.participants[("TEST-123", "b" * 32)]
    await wait_for(lambda: part_a["samples"] > 48000 and part_b["samples"] > 48000)
    assert part_a is not part_b
    # Roughly equal real-time streams; each is well below the sum of both.
    total = part_a["samples"] + part_b["samples"]
    assert part_a["samples"] < 0.75 * total and part_b["samples"] < 0.75 * total
