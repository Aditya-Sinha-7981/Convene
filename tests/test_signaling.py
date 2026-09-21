"""The phone signaling WebSocket, message by message (docs/transport.md). Synthetic phones, loopback only.

These carry over the intent of the CON-01 prototype characterization tests (join validation, identity reuse,
malformed input, cleanup on close) against the Convene contract. They prove the server's protocol handling,
not browser behavior.
"""
import asyncio

import pytest
from aiohttp import ClientSession, WSMsgType

from server.repositories import audit_events, connections, devices, participants
from tests.support.synthetic_phone import SyntheticPhone
from tests.support.util import wait_for
from tests.test_api_contract_docs import SIGNAL_CLIENT, SIGNAL_ERRORS, SIGNAL_SERVER

UNKNOWN = "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18"


@pytest.fixture
async def meeting_id(server):
    async with ClientSession() as http, http.post(server.base_url + "/api/meetings", json={}) as response:
        return (await response.json())["meeting"]["meeting_id"]


@pytest.fixture
def phone(server, meeting_id, make_phone):
    return make_phone(meeting_id)


async def closed_with(phone, timeout=5.0):
    """Wait for the server to close the socket; returns the close code."""
    message = await asyncio.wait_for(phone._ws.receive(), timeout)
    assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED), message
    return phone._ws.close_code


async def fatal(phone, message_or_raw, code):
    """Send something the server must reject fatally: an error reply naming ``code``, then close 4400."""
    if isinstance(message_or_raw, dict):
        await phone.send(message_or_raw)
    elif isinstance(message_or_raw, bytes):
        await phone._ws.send_bytes(message_or_raw)
    else:
        await phone._ws.send_str(message_or_raw)
    reply = await phone.receive()
    assert reply["type"] == "error" and reply["code"] == code and reply["fatal"] is True and reply["message"]
    assert await closed_with(phone) == 4400


def audit_codes(server):
    return [e.payload["code"] for e in audit_events.list_events(server.runtime.db.conn, event_type="signaling_error")]


# --- documented catalog ---------------------------------------------------------------------


def test_every_error_code_the_server_can_send_is_documented():
    from server.transport import peers, signaling
    import inspect
    import re
    used = set(re.findall(r'TransportError\(\s*"([a-z_]+)"', inspect.getsource(peers) + inspect.getsource(signaling)))
    assert used <= set(SIGNAL_ERRORS), sorted(used - set(SIGNAL_ERRORS))
    assert set(SIGNAL_CLIENT) == {"join", "offer", "leave"} and "joined" in SIGNAL_SERVER


# --- join -----------------------------------------------------------------------------------


async def test_join_attaches_a_registered_device(server, phone):
    status, body = await phone.register()
    assert status == 201
    await phone.open()
    joined = await phone.join()
    (person,) = body["device"]["participants"]
    assert joined == {"type": "joined", "device_id": phone.device_id, "participant_ids": [person["participant_id"]],
                      "device_status": "joining", "reconnect_count": 0, "is_reconnect": False, "peer_active": False}


async def test_join_for_an_unregistered_device_is_rejected_and_audited(server, phone, meeting_id):
    await phone.open()
    await fatal(phone, {"type": "join", "device_id": phone.device_id}, "device_not_registered")
    assert audit_codes(server) == ["device_not_registered"]
    event = audit_events.list_events(server.runtime.db.conn, event_type="signaling_error")[0]
    assert event.meeting_id == meeting_id and event.payload["device_id"] is None
    assert event.payload["remote_addr"] == "127.0.0.1"
    assert devices.get(server.runtime.db.conn, phone.device_id) is None  # a join never creates a device


async def test_join_for_a_device_registered_in_another_meeting_is_rejected(server, phone, make_phone):
    async with ClientSession() as http, http.post(server.base_url + "/api/meetings", json={}) as response:
        other = (await response.json())["meeting"]["meeting_id"]
    stranger = make_phone(other)
    await stranger.register()
    phone.device_id = stranger.device_id  # the same id, but this phone's meeting never registered it
    await phone.open()
    await fatal(phone, {"type": "join", "device_id": phone.device_id}, "device_not_registered")


@pytest.mark.parametrize("device_id", [None, 5, "not-a-uuid", "0D4F6A52-7C1B-4E7A-B0A3-51E1F4C2A9D8"])
async def test_join_with_an_invalid_device_id_is_invalid_message(server, phone, device_id):
    await phone.open()
    await fatal(phone, {"type": "join", "device_id": device_id}, "invalid_message")


async def test_join_for_an_ended_meeting_is_rejected(server, phone, meeting_id):
    await phone.register()
    async with ClientSession() as http, http.post(f"{server.base_url}/api/meetings/{meeting_id}/end"):
        pass
    await phone.open()
    await fatal(phone, {"type": "join", "device_id": phone.device_id}, "meeting_ended")


@pytest.mark.parametrize("path", [UNKNOWN, "not-a-uuid"])
async def test_unknown_meeting_is_an_error_frame_then_close(server, make_phone, path):
    stranger = make_phone(path)
    await stranger.open()
    reply = await stranger.receive()
    assert reply["type"] == "error" and reply["code"] == "meeting_not_found" and reply["fatal"] is True
    assert await closed_with(stranger) == 4400
    event = audit_events.list_events(server.runtime.db.conn, event_type="signaling_error")[0]
    assert event.meeting_id is None  # there is no meeting to attach it to


async def test_a_socket_cannot_switch_to_another_device(server, phone, make_phone, meeting_id):
    other = make_phone(meeting_id)
    await phone.register()
    await other.register()
    await phone.open()
    await phone.join()
    await fatal(phone, {"type": "join", "device_id": other.device_id}, "invalid_message")


# --- malformed and out-of-order messages ----------------------------------------------------


async def test_offer_and_leave_before_join_are_not_joined(server, phone):
    await phone.open()
    await fatal(phone, {"type": "offer", "sdp": "v=0"}, "not_joined")
    second = SyntheticPhone(server.base_url, phone.meeting_id)
    try:
        await second.open()
        await fatal(second, {"type": "leave"}, "not_joined")
    finally:
        await second.close()


@pytest.mark.parametrize("message", [{"type": "ice-candidate", "candidate": "x"}, {"type": "reconnect", "device_id": UNKNOWN},
                                     {"type": "ping"}, {"type": "answer", "sdp": "v=0"}])
async def test_unknown_or_server_only_types_are_rejected(server, phone, message):
    await phone.register()
    await phone.open()
    await phone.join()
    await fatal(phone, message, "unknown_message_type")


@pytest.mark.parametrize("raw", ["not json", "null", "[]", '"join"', "42", '{"type": 5}', "{}"])
async def test_non_object_or_non_json_text_is_invalid_message(server, phone, raw):
    await phone.open()
    await fatal(phone, raw, "invalid_message")


async def test_binary_frames_are_rejected_not_ignored(server, phone):
    await phone.open()
    await fatal(phone, b"\x00\x01\x02", "invalid_message")


async def test_an_oversized_message_is_rejected(server, phone):
    await phone.open()
    await fatal(phone, '{"type": "join", "pad": "' + "x" * 200_000 + '"}', "invalid_message")


async def test_a_fatal_error_is_audited_with_the_offending_device(server, phone):
    await phone.register()
    await phone.open()
    await phone.join()
    await fatal(phone, "garbage", "invalid_message")
    await wait_for(lambda: audit_codes(server))
    event = audit_events.list_events(server.runtime.db.conn, event_type="signaling_error")[0]
    assert event.payload["device_id"] == phone.device_id and event.payload["code"] == "invalid_message"


# --- offers ---------------------------------------------------------------------------------


@pytest.mark.parametrize("sdp", [None, 123, "this is not sdp", "v=0\r\ns=-\r\nt=0 0\r\n", "x" * 100_001])
async def test_bad_sdp_is_invalid_sdp(server, phone, sdp):
    await phone.register()
    await phone.open()
    await phone.join()
    await fatal(phone, {"type": "offer", "sdp": sdp}, "invalid_sdp")
    assert server.runtime.peers.sessions[phone.device_id].peer is None  # no peer is left behind


async def test_ice_restart_is_a_non_fatal_error_and_the_socket_stays_usable(server, phone):
    await phone.register()
    await phone.open()
    await phone.join()
    await phone.send({"type": "offer", "sdp": "v=0", "ice_restart": True})
    reply = await phone.receive()
    assert (reply["code"], reply["fatal"]) == ("no_active_peer", False)  # nothing to restart yet

    await phone.offer()
    await phone.wait_connected()
    await phone.send({"type": "offer", "sdp": "v=0", "ice_restart": True})
    reply = await phone.receive()
    assert (reply["code"], reply["fatal"]) == ("renegotiation_failed", False)
    session = server.runtime.peers.sessions[phone.device_id]
    frames = session.stats.frames
    await wait_for(lambda: session.stats.frames > frames + 20, message="the original peer must keep streaming")
    assert phone._ws.closed is False and session.peer.connectionState == "connected"
    await phone.send({"type": "join", "device_id": phone.device_id})
    assert (await phone.receive())["type"] == "joined"  # and the socket still works


async def test_ice_restart_must_be_a_boolean(server, phone):
    await phone.register()
    await phone.open()
    await phone.join()
    await fatal(phone, {"type": "offer", "sdp": "v=0", "ice_restart": "yes"}, "invalid_message")


async def test_a_valid_offer_gets_an_answer_and_the_device_connects(server, phone, meeting_id):
    await phone.register()
    await phone.open()
    await phone.join()
    answer = await phone.offer()
    assert answer["type"] == "answer" and "m=audio" in answer["sdp"]
    await phone.wait_connected()
    conn = server.runtime.db.conn
    await wait_for(lambda: devices.get(conn, phone.device_id).status == "connected")
    assert [e.event_type for e in connections.list_for_device(conn, phone.device_id)] == ["connected"]


# --- identity and reconnect at the signaling layer ------------------------------------------


async def test_a_second_socket_replaces_the_first(server, phone):
    await phone.register()
    await phone.open()
    await phone.join()
    first = phone._ws
    second = SyntheticPhone(server.base_url, phone.meeting_id, device_id=phone.device_id)
    try:
        await second.open()
        joined = await second.join()
        assert joined["type"] == "joined" and joined["participant_ids"] == phone.joined["participant_ids"]
        message = await asyncio.wait_for(first.receive(), 5)
        assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
        assert first.close_code == 1000  # displaced normally, not as an error
        assert server.runtime.peers.sessions[phone.device_id].ws is not None
        assert len(server.runtime.peers.sessions) == 1
    finally:
        await second.close()


async def test_two_devices_are_two_participants(server, phone, make_phone, meeting_id):
    other = make_phone(meeting_id, name="Sam")
    for p in (phone, other):
        await p.register()
        await p.open()
        await p.join()
    assert phone.joined["participant_ids"] != other.joined["participant_ids"]
    assert len(participants.list_for_meeting(server.runtime.db.conn, meeting_id)) == 2


async def test_closing_the_socket_before_any_offer_leaves_no_peer_and_no_event(server, phone):
    await phone.register()
    await phone.open()
    await phone.join()
    await phone.drop_signaling()
    session = server.runtime.peers.sessions[phone.device_id]
    await wait_for(lambda: session.ws is None)
    assert session.peer is None and devices.get(server.runtime.db.conn, phone.device_id).status == "joining"
    assert connections.list_for_device(server.runtime.db.conn, phone.device_id) == []


async def test_leave_closes_the_peer_marks_the_device_left_and_closes_the_socket(server, phone):
    await phone.connect()
    await phone.wait_connected()
    conn = server.runtime.db.conn
    await wait_for(lambda: devices.get(conn, phone.device_id).status == "connected")
    await phone.send({"type": "leave"})
    assert await closed_with(phone) == 1000
    await wait_for(lambda: devices.get(conn, phone.device_id).status == "left")
    session = server.runtime.peers.sessions[phone.device_id]
    assert session.peer is None and session.receive_task is None
    left = audit_events.list_events(conn, event_type="device_left")
    assert [(e.payload["reason"], e.payload["device_id"]) for e in left] == [("client_leave", phone.device_id)]


async def test_a_left_device_can_join_again_as_the_same_participant(server, phone):
    await phone.connect()
    await phone.wait_connected()
    await phone.send({"type": "leave"})
    await closed_with(phone)
    await phone.close()
    again = SyntheticPhone(server.base_url, phone.meeting_id, device_id=phone.device_id)
    try:
        await again.open()
        joined = await again.join()
        assert joined["device_status"] == "left" and joined["is_reconnect"] is True
        assert joined["participant_ids"] == phone.joined["participant_ids"]
    finally:
        await again.close()
