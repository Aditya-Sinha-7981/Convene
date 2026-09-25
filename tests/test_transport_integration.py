"""Transport integration with synthetic phones: audio sink, identity, isolation, meeting end, restart, hub, TLS.

Everything here runs over loopback with aiortc as the client. It shows the server's receive, cleanup, identity
and audit code works against a WebRTC peer. It does NOT establish browser, phone, Wi-Fi, HTTPS-trust,
screen-lock, or latency behavior; those are the real-phone checks in logs/transport.md.
"""
import asyncio
import json

import numpy as np
import pytest
from aiohttp import ClientSession, WSMsgType

from server import app as app_module
from server import registry
from server.dashboard_hub import OVERFLOW_CLOSE_CODE, DashboardHub
from server.repositories import audit_events, connections, devices, meetings, participants
from server.runtime import TransportConfig
from server.transport import peers as peers_module
from tests.support.certs import make_certificate
from tests.support.server import settings_in, start_server
from tests.support.synthetic_phone import SyntheticPhone
from tests.support.util import wait_for
from tests.test_api_contract_docs import DASHBOARD, GAUGE_KEYS


async def new_meeting(server, title="Sprint planning") -> str:
    async with ClientSession() as http, http.post(server.base_url + "/api/meetings", json={"title": title}) as r:
        return (await r.json())["meeting"]["meeting_id"]


def conn_of(server):
    return server.runtime.db.conn


def types(server, meeting_id=None):
    return [e.event_type for e in audit_events.list_events(conn_of(server), meeting_id=meeting_id)]


async def connected(server, meeting_id, make_phone, **kwargs) -> SyntheticPhone:
    phone = make_phone(meeting_id, **kwargs)
    await phone.connect()
    await phone.wait_connected()
    conn = conn_of(server)
    await wait_for(lambda: devices.get(conn, phone.device_id).status == "connected")
    return phone


class RecordingSink:
    def __init__(self):
        self.frames: dict[str, list] = {}

    async def push(self, device_id, pcm, sample_rate, t_wall):
        self.frames.setdefault(device_id, []).append((pcm, sample_rate, t_wall))


# --- audio and the sink seam ----------------------------------------------------------------


async def test_audio_reaches_the_sink_as_mono_pcm_with_a_wall_clock_time(settings):
    import time
    sink = RecordingSink()
    server = await start_server(settings, sink=sink)
    phones = []
    try:
        meeting_id = await new_meeting(server)
        phone = SyntheticPhone(server.base_url, meeting_id)
        phones.append(phone)
        await phone.connect()
        await phone.wait_connected()
        await wait_for(lambda: len(sink.frames.get(phone.device_id, [])) >= 50, message="frames must reach the sink")
        pcm, rate, t_wall = sink.frames[phone.device_id][-1]
        assert pcm.dtype == np.dtype("<i2") and pcm.ndim == 1 and rate == 48000
        assert 0 < len(pcm) <= 960 and abs(time.time() - t_wall) < 5
        assert np.abs(pcm).max() > 1000  # the tone survived Opus and the downmix
        times = [f[2] for f in sink.frames[phone.device_id]]
        assert times == sorted(times)
    finally:
        for p in phones:
            await p.close()
        await server.stop()


async def test_the_default_sink_counts_and_gauges_report(server, make_phone):
    meeting_id = await new_meeting(server)
    phone = await connected(server, meeting_id, make_phone)
    sink = server.runtime.sink
    await wait_for(lambda: sink.samples.get(phone.device_id, 0) >= 48000)
    async with ClientSession() as http, http.get(f"{server.base_url}/api/meetings/{meeting_id}") as r:
        (device,) = (await r.json())["devices"]
    gauges = device["gauges"]
    assert set(gauges) == GAUGE_KEYS
    assert gauges["audio_duration_s"] >= 1.0 and 0 <= gauges["last_audio_age_ms"] < 2000
    before = gauges["audio_duration_s"]
    await wait_for(lambda: server.runtime.peers.sessions[phone.device_id].stats.seconds > before + 0.5)


async def test_a_slow_sink_drops_that_devices_frames_but_never_blocks_receiving(settings):
    class StuckForA:
        def __init__(self):
            self.a_id = None
            self.b_frames = 0

        async def push(self, device_id, pcm, sample_rate, t_wall):
            if device_id == self.a_id:
                await asyncio.sleep(3600)  # A's sink hangs forever
            else:
                self.b_frames += 1

    sink = StuckForA()
    server = await start_server(settings, sink=sink, transport=TransportConfig(
        gauge_interval_s=0.2, metrics_log_interval_s=0, audio_queue_frames=5))
    phones = []
    try:
        meeting_id = await new_meeting(server)
        a, b = SyntheticPhone(server.base_url, meeting_id), SyntheticPhone(server.base_url, meeting_id)
        phones += [a, b]
        sink.a_id = a.device_id
        for p in (a, b):
            await p.connect()
        for p in (a, b):
            await p.wait_connected()
        sa, sb = (server.runtime.peers.sessions[p.device_id] for p in (a, b))
        await wait_for(lambda: sa.stats.dropped_frames > 20, message="A's overflow must be dropped and counted")
        received = sa.stats.frames
        await wait_for(lambda: sa.stats.frames > received + 20, message="A's receive loop must keep receiving")
        assert sa.stats.dropped_frames > 0 and sb.stats.dropped_frames == 0
        await wait_for(lambda: sink.b_frames > 50, message="B must be untouched by A's hung sink")
        assert sa.stats.last_audio_at is not None and sa.receive_task is not None and not sa.receive_task.done()
    finally:
        for p in phones:
            await p.close()
        await server.stop()


async def test_a_failing_sink_is_counted_and_does_not_stop_audio(settings):
    class Exploding:
        async def push(self, device_id, pcm, sample_rate, t_wall):
            raise RuntimeError("sink bug")

    server = await start_server(settings, sink=Exploding())
    phone = None
    try:
        meeting_id = await new_meeting(server)
        phone = SyntheticPhone(server.base_url, meeting_id)
        await phone.connect()
        await phone.wait_connected()
        session = server.runtime.peers.sessions[phone.device_id]
        await wait_for(lambda: session.stats.sink_errors > 10)
        frames = session.stats.frames
        await wait_for(lambda: session.stats.frames > frames + 20)
        assert session.peer.connectionState == "connected"
    finally:
        if phone:
            await phone.close()
        await server.stop()


async def test_a_gap_in_the_audio_is_recorded_as_audio_resumed(settings):
    server = await start_server(settings, transport=TransportConfig(gauge_interval_s=0.2, metrics_log_interval_s=0,
                                                                    audio_gap_s=0.4))
    phone = None
    try:
        meeting_id = await new_meeting(server)
        phone = SyntheticPhone(server.base_url, meeting_id)
        await phone.connect()
        await phone.wait_connected()
        session = server.runtime.peers.sessions[phone.device_id]
        await wait_for(lambda: session.stats.frames > 10)
        real_recv = phone.track.recv
        stalled = {"on": True}

        async def stalling_recv():  # the phone's mic goes quiet for a while, then comes back
            if stalled["on"]:
                stalled["on"] = False
                await asyncio.sleep(1.0)
            return await real_recv()

        phone.track.recv = stalling_recv
        await wait_for(lambda: session.stats.gaps >= 1, message="the gap must be noticed")
        conn = conn_of(server)
        await wait_for(lambda: any(e.event_type == "audio_resumed" for e in connections.list_for_device(conn, phone.device_id)))
        (event,) = audit_events.list_events(conn, event_type="device_audio_resumed")
        assert 400 <= event.payload["gap_ms"] < 3000 and event.payload["device_id"] == phone.device_id
        assert session.stats.gaps == 1  # one gap, one event
    finally:
        if phone:
            await phone.close()
        await server.stop()


# --- identity, reconnect, and socket-versus-peer lifetime -----------------------------------


@pytest.mark.parametrize("close_old_peer", [False, True], ids=["silent-drop", "page-closes-its-peer-first"])
async def test_reconnect_keeps_the_device_and_participant_and_counts_it(server, make_phone, close_old_peer):
    meeting_id = await new_meeting(server)
    phone = await connected(server, meeting_id, make_phone, user_agent="TestPhone/1")
    conn = conn_of(server)
    person = participants.list_for_device(conn, phone.device_id)
    old_peer = server.runtime.peers.sessions[phone.device_id].peer

    await phone.reconnect(close_old_peer=close_old_peer)  # a new socket and a fresh peer, as the page does
    await phone.wait_connected()
    assert phone.joined is not None
    await wait_for(lambda: devices.get(conn, phone.device_id).reconnect_count == 1, message="reconnect must be counted")

    assert participants.list_for_device(conn, phone.device_id) == person  # never a second participant
    assert len(devices.list_for_meeting(conn, meeting_id)) == 1
    await wait_for(lambda: old_peer.connectionState == "closed")
    (event,) = audit_events.list_events(conn, event_type="device_reconnected")
    assert event.payload == {"device_id": phone.device_id, "reconnect_count": 1, "via": "new_peer",
                             "remote_addr": "127.0.0.1", "user_agent": "TestPhone/1"}
    history = [e.event_type for e in connections.list_for_device(conn, phone.device_id)]
    if close_old_peer:
        # the server saw the old peer close before the new one arrived: that is a real disconnect
        assert history == ["connected", "disconnected", "reconnected"]
        assert audit_events.list_events(conn, event_type="device_disconnected")[0].payload["reason"] == "peer_closed"
    else:
        assert history == ["connected", "reconnected"]  # the old peer was replaced, never observed to fail
    session = server.runtime.peers.sessions[phone.device_id]
    frames = session.stats.frames
    await wait_for(lambda: session.stats.frames > frames + 20, message="audio must resume on the new peer")
    assert types(server, meeting_id).count("meeting_started") == 1


async def test_a_second_socket_with_the_same_device_replaces_the_first_and_reconnects(server, make_phone):
    meeting_id = await new_meeting(server)
    first = await connected(server, meeting_id, make_phone)
    conn = conn_of(server)
    second = SyntheticPhone(server.base_url, meeting_id, name="ignored", device_id=first.device_id)
    try:
        await second.connect()
        await second.wait_connected()
        await wait_for(lambda: devices.get(conn, first.device_id).reconnect_count == 1)
        message = await asyncio.wait_for(first._ws.receive(), 5)  # aiohttp processes the close frame on read
        assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
        assert first._ws.close_code == 1000  # the older socket was closed by the server, normally
        assert len(server.runtime.peers.sessions) == 1
        assert len(participants.list_for_device(conn, first.device_id)) == 1
    finally:
        await second.close()


async def test_closing_the_signaling_socket_alone_does_not_disconnect_a_streaming_device(server, make_phone):
    """ADR-16: media can outlive signaling; device status follows the peer connection."""
    meeting_id = await new_meeting(server)
    phone = await connected(server, meeting_id, make_phone)
    session = server.runtime.peers.sessions[phone.device_id]
    await phone.drop_signaling()
    await wait_for(lambda: session.ws is None)
    frames = session.stats.frames
    await wait_for(lambda: session.stats.frames > frames + 30, message="audio must keep flowing without a socket")
    assert devices.get(conn_of(server), phone.device_id).status == "connected"
    assert "device_disconnected" not in types(server, meeting_id)


async def test_closing_the_peer_records_a_disconnect_and_releases_the_device(server, make_phone):
    meeting_id = await new_meeting(server)
    phone = await connected(server, meeting_id, make_phone)
    conn = conn_of(server)
    session = server.runtime.peers.sessions[phone.device_id]
    await phone.peer.close()  # the phone's page closed its RTCPeerConnection
    await wait_for(lambda: devices.get(conn, phone.device_id).status == "disconnected", timeout=15,
                   message="the server must notice a closed peer without help from the client")
    (event,) = audit_events.list_events(conn, event_type="device_disconnected")
    assert event.payload["reason"] == "peer_closed" and event.payload["device_id"] == phone.device_id
    await wait_for(lambda: session.peer is None and session.receive_task is None)
    assert [e.event_type for e in connections.list_for_device(conn, phone.device_id)] == ["connected", "disconnected"]


async def test_a_disconnected_device_returns_under_the_same_identity(server, make_phone):
    meeting_id = await new_meeting(server)
    phone = await connected(server, meeting_id, make_phone)
    conn = conn_of(server)
    person = participants.list_for_device(conn, phone.device_id)
    await phone.peer.close()
    await wait_for(lambda: devices.get(conn, phone.device_id).status == "disconnected", timeout=15)
    await phone.reconnect()
    await phone.wait_connected()
    await wait_for(lambda: devices.get(conn, phone.device_id).status == "connected")
    assert devices.get(conn, phone.device_id).reconnect_count == 1
    assert participants.list_for_device(conn, phone.device_id) == person


# --- isolation ------------------------------------------------------------------------------


async def test_one_devices_malformed_message_and_disconnect_leave_the_other_streaming(server, make_phone):
    meeting_id = await new_meeting(server)
    a = await connected(server, meeting_id, make_phone, name="A")
    b = await connected(server, meeting_id, make_phone, name="B")
    conn = conn_of(server)
    sa = server.runtime.peers.sessions[a.device_id]
    frames = sa.stats.frames

    await b.send({"type": "join", "device_id": "not-a-uuid"})  # B misbehaves
    reply = await b.receive()
    assert reply["code"] == "invalid_message" and reply["fatal"]
    await wait_for(lambda: devices.get(conn, b.device_id).status == "disconnected", timeout=15,
                   message="B's connection is reset")
    await wait_for(lambda: sa.stats.frames > frames + 30, message="A must keep receiving")
    assert devices.get(conn, a.device_id).status == "connected"
    assert sa.peer.connectionState == "connected"
    assert [e.event_type for e in connections.list_for_device(conn, a.device_id)] == ["connected"]

    frames = sa.stats.frames
    await b.close()  # and B going away entirely changes nothing for A
    await wait_for(lambda: sa.stats.frames > frames + 30)
    assert devices.get(conn, a.device_id).reconnect_count == 0


async def test_an_exception_in_one_devices_receive_loop_is_audited_and_isolated(server, make_phone, monkeypatch):
    meeting_id = await new_meeting(server)
    a = await connected(server, meeting_id, make_phone, name="A")
    b = await connected(server, meeting_id, make_phone, name="B")
    conn = conn_of(server)
    peers = server.runtime.peers
    sa, sb = peers.sessions[a.device_id], peers.sessions[b.device_id]
    real = peers_module.frame_to_mono_int16

    def explode_for_b(frame):
        if asyncio.current_task() is sb.receive_task:
            raise RuntimeError("decoder blew up for B only")
        return real(frame)

    monkeypatch.setattr(peers_module, "frame_to_mono_int16", explode_for_b)
    frames = sa.stats.frames
    await wait_for(lambda: devices.get(conn, b.device_id).status == "disconnected", timeout=15,
                   message="B's failure becomes B's disconnect")
    events = audit_events.list_events(conn, event_type="signaling_error")
    assert [(e.payload["code"], e.payload["device_id"]) for e in events] == [("receive_failed", b.device_id)]
    (down,) = audit_events.list_events(conn, event_type="device_disconnected")
    assert down.payload["reason"] == "peer_failed"
    await wait_for(lambda: sa.stats.frames > frames + 30, message="A keeps streaming")
    assert devices.get(conn, a.device_id).status == "connected" and not sa.receive_task.done()
    async with ClientSession() as http, http.get(server.base_url + "/metrics") as response:
        assert response.status == 200  # the server is alive


# --- ending the meeting ---------------------------------------------------------------------


async def test_ending_a_meeting_closes_peers_persists_state_and_calls_the_hook(server, make_phone):
    meeting_id = await new_meeting(server)
    a = await connected(server, meeting_id, make_phone, name="A")
    b = await connected(server, meeting_id, make_phone, name="B")
    called = []

    async def hook(mid):
        called.append(mid)

    server.runtime.on_meeting_ended(hook, "test-hook")
    peers = [server.runtime.peers.sessions[p.device_id].peer for p in (a, b)]
    async with ClientSession() as http, http.post(f"{server.base_url}/api/meetings/{meeting_id}/end") as response:
        assert response.status == 202
    assert called == [meeting_id]
    for phone in (a, b):
        assert (await phone.receive())["type"] == "meeting_ended"  # the phone is told, and will not retry
        message = await asyncio.wait_for(phone._ws.receive(), 5)
        assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED) and phone._ws.close_code == 1000
    assert all(p.connectionState == "closed" for p in peers)
    conn = conn_of(server)
    assert meetings.get(conn, meeting_id).status == "ended" and meetings.get(conn, meeting_id).ended_at
    assert {devices.get(conn, p.device_id).status for p in (a, b)} == {"left"}
    assert server.runtime.peers.sessions == {}
    ordered = types(server, meeting_id)
    assert ordered.count("meeting_ended") == 1 and ordered.count("device_left") == 2
    assert "device_disconnected" not in ordered  # teardown is not a disconnect
    async with ClientSession() as http, http.post(f"{server.base_url}/api/meetings/{meeting_id}/end") as response:
        assert response.status == 200
    assert called == [meeting_id]  # the hook is not called twice for an idempotent end


async def test_a_hook_that_raises_or_hangs_is_audited_and_does_not_fail_the_request(settings):
    server = await start_server(settings, transport=TransportConfig(gauge_interval_s=0.2, metrics_log_interval_s=0,
                                                                    hook_timeout_s=0.3))
    try:
        meeting_id = await new_meeting(server)
        ran = []

        async def raises(mid):
            raise RuntimeError("summary trigger exploded")

        async def hangs(mid):
            await asyncio.sleep(3600)

        async def fine(mid):
            ran.append(mid)

        for name, hook in (("raises", raises), ("hangs", hangs), ("fine", fine)):
            server.runtime.on_meeting_ended(hook, name)
        async with ClientSession() as http, http.post(f"{server.base_url}/api/meetings/{meeting_id}/end") as response:
            assert response.status == 202
            assert (await response.json())["meeting"]["status"] == "ended"
        failures = audit_events.list_events(conn_of(server), event_type="hook_failed")
        assert [(e.payload["hook"], e.meeting_id) for e in failures] == [("raises", meeting_id), ("hangs", meeting_id)]
        assert "summary trigger exploded" in failures[0].payload["error"] and "Timeout" in failures[1].payload["error"]
        assert ran == [meeting_id]  # a failing hook does not stop the ones after it
    finally:
        await server.stop()


async def test_a_late_offer_for_an_ended_meeting_creates_no_peer(server, make_phone):
    meeting_id = await new_meeting(server)
    phone = make_phone(meeting_id)
    await phone.register()
    await phone.open()
    await phone.join()  # joined, offer not yet sent
    async with ClientSession() as http, http.post(f"{server.base_url}/api/meetings/{meeting_id}/end"):
        pass
    assert (await phone.receive())["type"] == "meeting_ended"
    late = SyntheticPhone(server.base_url, meeting_id, device_id=phone.device_id)
    try:
        await late.open()
        reply = await late.join()
        assert reply["code"] == "meeting_ended"
    finally:
        await late.close()
    assert server.runtime.peers.sessions == {}


# --- restart --------------------------------------------------------------------------------


async def test_a_registered_device_reconnects_after_a_server_restart_under_the_same_identity(tmp_path):
    settings = settings_in(tmp_path)
    first = await start_server(settings)
    phone = second_phone = second = None
    try:
        meeting_id = await new_meeting(first)
        phone = SyntheticPhone(first.base_url, meeting_id, name="Priya")
        await phone.connect()
        await phone.wait_connected()
        await wait_for(lambda: devices.get(conn_of(first), phone.device_id).status == "connected")
        person = participants.list_for_device(conn_of(first), phone.device_id)
    finally:
        await first.stop()  # the server goes away while the phone is still connected, as in a crash or restart
        await phone.close()

    second = await start_server(settings)  # same database file, new process-equivalent
    try:
        conn = conn_of(second)
        assert devices.get(conn, phone.device_id).status == "disconnected"  # reconciliation ran before serving
        (down,) = audit_events.list_events(conn, event_type="device_disconnected")
        assert down.payload["reason"] == "server_restart"
        assert meetings.get(conn, meeting_id).status == "live"

        second_phone = SyntheticPhone(second.base_url, meeting_id, name="Priya", device_id=phone.device_id)
        status, body = await second_phone.register()  # the page registers again on load: a replay
        assert status == 200 and body["device"]["participants"][0]["participant_id"] == person[0].participant_id
        await second_phone.open()
        joined = await second_phone.join()
        assert joined["is_reconnect"] is True and joined["participant_ids"] == [person[0].participant_id]
        await second_phone.offer()
        await second_phone.wait_connected()
        await wait_for(lambda: devices.get(conn, phone.device_id).reconnect_count == 1)
        assert participants.list_for_device(conn, phone.device_id) == person
        assert audit_events.list_events(conn, event_type="device_reconnected")[0].payload["via"] == "new_peer"
    finally:
        if second_phone:
            await second_phone.close()
        await second.stop()


# --- dashboard hub --------------------------------------------------------------------------


async def dashboard(server, meeting_id):
    session = ClientSession()
    ws = await session.ws_connect(server.ws_url(f"/ws/dashboard/{meeting_id}"))
    return session, ws


async def drain(ws, seconds=0.5):
    messages = []
    end = asyncio.get_running_loop().time() + seconds
    while (left := end - asyncio.get_running_loop().time()) > 0:
        try:
            message = await asyncio.wait_for(ws.receive(), left)
        except asyncio.TimeoutError:
            break
        if message.type == WSMsgType.TEXT:
            messages.append(json.loads(message.data))
    return messages


async def test_dashboard_receives_meeting_and_connection_events_for_its_meeting_only(server, make_phone):
    mine, other = await new_meeting(server, "mine"), await new_meeting(server, "other")
    s1, mine_ws = await dashboard(server, mine)
    s2, other_ws = await dashboard(server, other)
    try:
        phone = make_phone(mine, name="Priya")
        await phone.connect()
        await phone.wait_connected()
        await asyncio.sleep(0.6)
        seen = await drain(mine_ws, 0.8)
        kinds = [m["type"] for m in seen]
        assert {"meeting_status", "device_status", "connection_event", "device_gauges"} <= set(kinds)
        for m in seen:
            assert m["type"] in DASHBOARD and m["meeting_id"] == mine
            durable = DASHBOARD[m["type"]][0]
            assert (m["seq"] is None) == (not durable)
        durable_seqs = [m["seq"] for m in seen if m["seq"] is not None]
        assert durable_seqs == sorted(durable_seqs) and len(set(durable_seqs)) >= 3
        live = next(m for m in seen if m["type"] == "meeting_status")
        assert live["meeting"]["status"] == "live"
        connected_event = next(m for m in seen if m["type"] == "connection_event")
        assert connected_event["event"]["event_type"] == "connected" and connected_event["event"]["device_id"] == phone.device_id
        status_event = [m for m in seen if m["type"] == "device_status"][-1]
        assert status_event["device"]["status"] == "connected"
        assert status_event["device"]["participants"][0]["display_name"] == "Priya"
        gauges = [m for m in seen if m["type"] == "device_gauges"]
        assert gauges and set(gauges[0]["gauges"][0]) == GAUGE_KEYS | {"device_id"}
        # events are the audit stream, not a second log: every durable seq exists in it
        stored = {e.seq for e in audit_events.list_events(conn_of(server), meeting_id=mine)}
        assert set(durable_seqs) <= stored
        assert await drain(other_ws, 0.3) == []  # a different meeting's dashboard hears nothing
    finally:
        await mine_ws.close()
        await other_ws.close()
        await s1.close()
        await s2.close()


async def test_dashboard_is_read_only_and_reports_leave_and_end(server, make_phone):
    meeting_id = await new_meeting(server)
    session, ws = await dashboard(server, meeting_id)
    try:
        phone = await connected(server, meeting_id, make_phone)
        await drain(ws, 0.4)
        before = types(server)
        for junk in ('{"type": "join", "device_id": "x"}', "garbage", '{"type": "end"}'):
            await ws.send_str(junk)
        await ws.send_bytes(b"\x00")
        await asyncio.sleep(0.3)
        assert ws.closed is False and types(server) == before  # client frames change nothing
        assert devices.get(conn_of(server), phone.device_id).status == "connected"

        await phone.send({"type": "leave"})
        seen = await drain(ws, 1.0)
        left = [m for m in seen if m["type"] == "device_status" and m["device"]["status"] == "left"]
        assert left
        async with ClientSession() as http, http.post(f"{server.base_url}/api/meetings/{meeting_id}/end"):
            pass
        seen = await drain(ws, 1.0)
        assert any(m["type"] == "meeting_status" and m["meeting"]["status"] == "ended" for m in seen)
    finally:
        await ws.close()
        await session.close()


async def test_dashboard_for_an_unknown_meeting_gets_an_error_frame_then_a_normal_close(server):
    session, ws = await dashboard(server, "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18")
    try:
        frame = json.loads((await ws.receive()).data)
        assert frame["type"] == "error" and frame["code"] == "meeting_not_found" and frame["seq"] is None
        assert (await ws.receive()).type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
        assert ws.close_code == 1000
    finally:
        await session.close()


async def test_a_dashboard_that_cannot_keep_up_is_closed_with_1013_and_others_are_unaffected():
    class FakeSocket:
        def __init__(self):
            self.closed_with = None
            self.sent = []

        async def send_json(self, message):
            self.sent.append(message)

        async def close(self, code=1000):
            self.closed_with = code

    hub = DashboardHub(db=None, peers=None, client_queue=3)
    from server.dashboard_hub import _Client
    slow, healthy = _Client(FakeSocket(), 3), _Client(FakeSocket(), 100)
    hub._clients["m"] = {slow, healthy}
    for n in range(10):
        hub._broadcast("m", {"n": n})
    await asyncio.sleep(0.05)
    assert slow.ws.closed_with == OVERFLOW_CLOSE_CODE == 1013
    assert healthy.queue.qsize() == 10 and healthy.ws.closed_with is None


async def test_hub_events_carry_the_audit_seq_and_are_produced_by_the_emit_subscriber(server):
    meeting_id = await new_meeting(server)
    session, ws = await dashboard(server, meeting_id)
    try:
        async with ClientSession() as http:
            body = {"device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18", "display_name": "Sam", "is_shared": False}
            async with http.post(f"{server.base_url}/api/meetings/{meeting_id}/devices", json=body):
                pass
        (message,) = [m for m in await drain(ws, 0.6) if m["type"] == "device_status"]
        registered = audit_events.list_events(conn_of(server), event_type="device_registered")[0]
        assert message["seq"] == registered.seq and message["device"]["status"] == "joining"
        # an event emitted by any component reaches the hub the same way
        await server.runtime.db.run(lambda tx: registry.record_device_left(tx, "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18", "client_leave"))
        (left,) = [m for m in await drain(ws, 0.6) if m["type"] == "device_status"]
        assert left["seq"] > message["seq"] and left["device"]["status"] == "left"
    finally:
        await ws.close()
        await session.close()


# --- startup, TLS, and the entry point ------------------------------------------------------


async def test_the_app_serves_https_and_wss_with_a_local_certificate(tmp_path):
    cert, key = make_certificate(tmp_path, ips=["127.0.0.1"])
    server = await start_server(settings_in(tmp_path), ssl_certfile=str(cert), ssl_keyfile=str(key))
    try:
        async with ClientSession() as http:  # verification off: the throwaway certificate is not trusted
            async with http.post(server.base_url + "/api/meetings", json={}, ssl=False) as response:
                assert response.status == 201 and server.base_url.startswith("https://")
                meeting_id = (await response.json())["meeting"]["meeting_id"]
            async with http.ws_connect(server.ws_url(f"/ws/signal/{meeting_id}"), ssl=False) as ws:
                await ws.send_json({"type": "join", "device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18"})
                assert json.loads((await ws.receive()).data)["code"] == "device_not_registered"
    finally:
        await server.stop()


def test_startup_fails_loudly_when_the_certificate_lacks_the_advertised_address(tmp_path, monkeypatch, capsys):
    cert, key = make_certificate(tmp_path, ips=["192.168.1.5"], dns=["localhost"])
    monkeypatch.setattr(app_module.uvicorn, "run", lambda *a, **k: pytest.fail("the server must not start"))
    with pytest.raises(SystemExit) as caught:
        app_module.main(["--cert", str(cert), "--key", str(key), "--advertise-ip", "192.168.50.10", "--no-stt"])
    message = str(caught.value)
    assert "192.168.50.10" in message and "192.168.1.5" in message and "localhost" in message
    assert "startup failed" in message


def test_startup_reports_a_bad_advertise_ip_or_config_and_no_lan_address(tmp_path, monkeypatch, capsys):
    cert, key = make_certificate(tmp_path, ips=["192.168.50.10"])
    monkeypatch.setattr(app_module.uvicorn, "run", lambda *a, **k: pytest.fail("the server must not start"))
    with pytest.raises(SystemExit, match="startup failed"):
        app_module.main(["--cert", str(cert), "--key", str(key), "--advertise-ip", "999.1.1.1", "--no-stt"])
    bad_config = tmp_path / "bad.toml"
    bad_config.write_text("[paths")
    with pytest.raises(SystemExit, match="startup failed"):
        app_module.main(["--cert", str(cert), "--key", str(key), "--advertise-ip", "192.168.50.10",
                         "--config", str(bad_config), "--no-stt"])

    started = {}
    monkeypatch.setattr(app_module.uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    monkeypatch.setattr(app_module.network, "detect_lan_address", lambda advertise=None: None)
    app_module.main(["--cert", str(cert), "--key", str(key), "--no-stt"])  # no address: warns and still starts
    assert "No LAN address detected" in capsys.readouterr().err and started["host"] == "0.0.0.0"


def test_main_binds_all_interfaces_over_tls_with_the_documented_options(tmp_path, monkeypatch, capsys):
    cert, key = make_certificate(tmp_path, ips=["192.168.50.10"])
    started = {}
    monkeypatch.setattr(app_module.uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    app_module.main(["--cert", str(cert), "--key", str(key), "--port", "9443", "--advertise-ip", "192.168.50.10",
                     "--config", str(tmp_path / "absent.toml"), "--no-stt"])
    assert (started["host"], started["port"]) == ("0.0.0.0", 9443)
    assert started["ssl_certfile"] == str(cert) and started["ssl_keyfile"] == str(key)
    assert started["ws_ping_interval"] == 15 and started["ws_max_size"] == TransportConfig().ws_max_size
    out = capsys.readouterr().out
    assert "LAN address: 192.168.50.10" in out and "Certificate OK" in out and "https://192.168.50.10:9443/" in out
    assert started["app"].state.runtime.host == "192.168.50.10" and started["app"].state.runtime.port == 9443


def test_public_host_overrides_config_and_keeps_lan_address_for_dns_diagnostics(tmp_path, monkeypatch, capsys):
    cert, key = make_certificate(tmp_path, dns=["convene.example.com"])
    config = tmp_path / "convene.toml"
    config.write_text("[network]\npublic_host = 'from-config.example.com'\n")
    started = {}
    monkeypatch.setattr(app_module.uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    monkeypatch.setattr(app_module.network, "resolution_warning", lambda host, address: None)
    app_module.main(["--cert", str(cert), "--key", str(key), "--advertise-ip", "192.168.50.10",
                     "--public-host", "convene.example.com", "--config", str(config), "--no-stt"])
    assert started["app"].state.runtime.host == "convene.example.com"
    assert "Public join host: convene.example.com" in capsys.readouterr().out


def test_invalid_public_host_fails_before_server_starts(tmp_path, monkeypatch):
    cert, key = make_certificate(tmp_path, ips=["192.168.50.10"])
    monkeypatch.setattr(app_module.uvicorn, "run", lambda *a, **k: pytest.fail("the server must not start"))
    with pytest.raises(SystemExit, match="--public-host must be a hostname"):
        app_module.main(["--cert", str(cert), "--key", str(key), "--advertise-ip", "192.168.50.10",
                         "--public-host", "192.168.50.10", "--no-stt"])


def test_the_transport_never_configures_ice_servers():
    """ADR-10: no STUN/TURN, no public signaling."""
    import inspect
    source = inspect.getsource(peers_module)
    assert "iceServers=[]" in source and "stun:" not in source.lower() and "turn:" not in source.lower()


async def test_diagnostics_route_reports_per_device_counters(server, make_phone):
    meeting_id = await new_meeting(server)
    phone = await connected(server, meeting_id, make_phone)
    await wait_for(lambda: server.runtime.peers.sessions[phone.device_id].stats.frames > 10)
    async with ClientSession() as http, http.get(server.base_url + "/metrics") as response:
        (row,) = await response.json()
    assert row["device_id"] == phone.device_id and row["state"] == "connected" and row["audio_received"] is True
    assert {"frames", "dropped_frames", "sink_errors", "audio_gaps"} <= set(row) and GAUGE_KEYS <= set(row)


def test_startup_without_a_pinned_or_cached_model_fails_loudly_and_never_serves(tmp_path, monkeypatch):
    cert, key = make_certificate(tmp_path, ips=["192.168.50.10"])
    monkeypatch.setattr(app_module.uvicorn, "run", lambda *a, **k: pytest.fail("the server must not start"))
    with pytest.raises(SystemExit) as unpinned:  # no model pinned in the (absent) config
        app_module.main(["--cert", str(cert), "--key", str(key), "--advertise-ip", "192.168.50.10",
                         "--config", str(tmp_path / "absent.toml")])
    assert "startup failed" in str(unpinned.value) and "no STT model is pinned" in str(unpinned.value)

    config = tmp_path / "pinned.toml"  # pinned, but nothing is in the local cache
    config.write_text('[models.stt]\nmodel = "nobody/never-downloaded-model"\nrevision = "0000000000000000000000000000000000000000"\n')
    import huggingface_hub.constants as hf_constants  # its cache path is read once at import: patch the constant
    monkeypatch.setattr(hf_constants, "HF_HUB_CACHE", str(tmp_path / "empty-hf-cache"))
    monkeypatch.delenv("HF_HUB_OFFLINE", raising=False)
    with pytest.raises(SystemExit) as missing:
        app_module.main(["--cert", str(cert), "--key", str(key), "--advertise-ip", "192.168.50.10", "--config", str(config)])
    assert "not in the local cache" in str(missing.value) and "provision_models.py" in str(missing.value)


def test_no_stt_flag_skips_the_model_and_a_loaded_adapter_is_handed_to_the_app(tmp_path, monkeypatch, capsys):
    from server.pipeline.adapter import FakeAdapter
    cert, key = make_certificate(tmp_path, ips=["192.168.50.10"])
    started = {}
    monkeypatch.setattr(app_module.uvicorn, "run", lambda app, **kw: started.update(app=app, **kw))
    base = ["--cert", str(cert), "--key", str(key), "--advertise-ip", "192.168.50.10", "--config", str(tmp_path / "absent.toml")]
    app_module.main([*base, "--no-stt"])
    assert started["app"].state.runtime.stt_adapter is None and "STT disabled" in capsys.readouterr().out

    fake = FakeAdapter()
    fake.load_seconds = 1.5
    monkeypatch.setattr(app_module, "build_adapter", lambda config: fake)
    with pytest.raises(SystemExit, match="no reasoning model is pinned"):
        app_module.main(base)  # the absent config pins no reasoning model: startup refuses, loudly

    from server.rag.reasoning import FakeReasoningAdapter
    reasoning = FakeReasoningAdapter()
    reasoning.load_seconds = 2.5
    monkeypatch.setattr(app_module, "build_reasoning_adapter", lambda config: reasoning)
    app_module.main(base)
    out = capsys.readouterr().out
    assert fake.loaded is True and "STT model ready (1.5 s)" in out
    assert reasoning.loaded is True and "Reasoning model ready (2.5 s)" in out
    assert started["app"].state.runtime.stt_adapter is fake and started["app"].state.runtime._stt_loaded is True
    assert started["app"].state.runtime.reasoning_adapter is reasoning
    assert started["app"].state.runtime._reasoning_loaded is True
