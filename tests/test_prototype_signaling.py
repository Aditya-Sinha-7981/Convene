"""Characterization tests for the DT-17 prototype's signaling, identity and metrics.

These pin down what ``server/app.py`` does today (CON-01) so CON-04 can tell a regression
from a deliberate change. Where the prototype's behavior differs from ``docs/transport.md`` the test
documents the prototype and the migration map in ``logs/transport.md`` records the gap.
No phone, certificate or model is needed.
"""
import asyncio
import re

import pytest
from aiohttp import ClientSession, WSMsgType

from server import app as prototype
from tests.support.synthetic_phone import SyntheticPhone

TOKEN = "a" * 32
ID_PATTERN = re.compile(r"^p[0-9a-f]{6}$")


@pytest.fixture
async def phones(signaling_url):
    """Factory for synthetic phones that are closed at teardown."""
    made = []

    def make(token=TOKEN, url=None):
        phone = SyntheticPhone(url or signaling_url, token)
        made.append(phone)
        return phone

    yield make
    for phone in made:
        await phone.close()


async def get_metrics(server):
    async with ClientSession() as session:
        async with session.get(server.make_url("/metrics")) as response:
            assert response.status == 200
            assert response.content_type == "application/json"
            return await response.json()


# --- routes and process setup -------------------------------------------------------------


async def test_registered_routes_and_static_pages(server, meeting):
    async with ClientSession() as session:
        async with session.get(server.make_url(f"/join/{meeting}")) as response:
            assert response.status == 200
            assert "text/html" in response.content_type
        async with session.get(server.make_url("/app.js")) as response:
            assert response.status == 200
            assert "javascript" in response.content_type
        async with session.get(server.make_url("/metrics")) as response:
            assert response.status == 200
        async with session.get(server.make_url("/api/meetings")) as response:
            assert response.status == 404  # no Convene API exists yet (CON-04)


def test_prototype_serves_on_all_interfaces_with_tls(run_app_kwargs):
    assert run_app_kwargs["host"] == "0.0.0.0"
    assert run_app_kwargs["port"] == 8443
    assert run_app_kwargs["ssl_context"] is not None


# --- join validation ----------------------------------------------------------------------


@pytest.mark.parametrize("token", [None, "", "a" * 15, "a" * 129, 12345, ["a" * 20]],
                         ids=["missing", "empty", "15-chars", "129-chars", "non-string", "list"])
async def test_join_with_invalid_token_is_rejected_and_socket_stays_open(phones, token):
    phone = phones()
    await phone.open()
    message = {"type": "join"}
    if token is not None:
        message["token"] = token
    await phone.send(message)
    reply = await phone.receive()
    assert reply == {"type": "error", "message": "invalid session token"}
    assert prototype.participants == {}
    # The socket is still usable: a valid join on it succeeds.
    assert (await phone.join())["type"] == "joined"


@pytest.mark.parametrize("length", [16, 128])
async def test_join_accepts_token_length_boundaries(phones, length):
    phone = phones(token="b" * length)
    await phone.open()
    reply = await phone.join()
    assert reply["type"] == "joined"
    assert ID_PATTERN.match(reply["participantId"])
    assert reply["reconnects"] == 0


# --- malformed and out-of-order messages --------------------------------------------------


async def test_offer_before_join_is_an_error_not_a_crash(phones):
    phone = phones()
    await phone.open()
    await phone.send({"type": "offer", "sdp": "v=0"})
    assert (await phone.receive()) == {"type": "error", "message": "unexpected message"}
    assert prototype.participants == {}
    assert (await phone.join())["type"] == "joined"


async def test_unknown_message_type_after_join_is_an_error(phones):
    phone = phones()
    await phone.open()
    await phone.join()
    await phone.send({"type": "ice-candidate", "candidate": "x"})  # documented target, absent in prototype
    reply = await phone.receive()
    assert reply == {"type": "error", "message": "unexpected message"}


@pytest.mark.parametrize("raw", ["not json", "null", "[]", '"join"', "42"],
                         ids=["text", "null", "array", "string", "number"])
async def test_non_object_or_non_json_text_gets_error_reply_and_socket_survives(phones, raw):
    phone = phones()
    await phone.open()
    await phone._ws.send_str(raw)
    reply = await phone.receive()
    assert reply["type"] == "error"
    assert not phone._ws.closed
    assert (await phone.join())["type"] == "joined"


async def test_binary_frames_are_ignored(phones):
    phone = phones()
    await phone.open()
    await phone._ws.send_bytes(b"\x00\x01\x02")
    assert (await phone.join())["type"] == "joined"  # the next reply is the join reply, not an error


async def test_oversized_sdp_is_rejected(phones):
    phone = phones()
    await phone.open()
    await phone.join()
    await phone.send({"type": "offer", "sdp": "v" * 100001})
    assert (await phone.receive()) == {"type": "error", "message": "invalid offer"}
    assert prototype.participants[("TEST-123", TOKEN)]["peer"] is None


@pytest.mark.parametrize("sdp", [None, 123], ids=["missing", "non-string"])
async def test_non_string_sdp_is_rejected(phones, sdp):
    phone = phones()
    await phone.open()
    await phone.join()
    await phone.send({"type": "offer", "sdp": sdp})
    assert (await phone.receive()) == {"type": "error", "message": "invalid offer"}


async def test_garbage_sdp_is_answered_with_an_empty_answer_not_an_error(phones):
    """Observed gap: only the type and length of ``sdp`` are validated. A junk offer gets an
    empty answer (no media sections) and a peer stuck in ``new`` that never receives audio.
    docs/transport.md wants malformed input rejected at the transport layer (CON-04)."""
    phone = phones()
    await phone.open()
    await phone.join()
    await phone.send({"type": "offer", "sdp": "this is not sdp"})
    reply = await phone.receive()
    assert reply["type"] == "answer"
    assert "m=audio" not in reply["sdp"]
    participant = prototype.participants[("TEST-123", TOKEN)]
    assert participant["peer"].connectionState == "new"
    assert participant["audio_task"] is None
    assert not phone._ws.closed


# --- identity and reconnect counting ------------------------------------------------------


async def test_same_token_reuses_participant_and_counts_reconnect(phones):
    first = phones()
    await first.open()
    joined = await first.join()
    assert joined["reconnects"] == 0

    second = phones()
    await second.open()
    rejoined = await second.join()

    assert rejoined["participantId"] == joined["participantId"]
    assert rejoined["reconnects"] == 1
    # The first socket is closed by the server.
    message = await asyncio.wait_for(first._ws.receive(), 5)
    assert message.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED)
    assert len(prototype.participants) == 1


async def test_reconnect_count_increments_on_every_rejoin(phones):
    seen = []
    for _ in range(4):
        phone = phones()
        await phone.open()
        seen.append((await phone.join())["reconnects"])
    assert seen == [0, 1, 2, 3]


async def test_different_tokens_are_different_participants(phones):
    a, b = phones(token="a" * 32), phones(token="b" * 32)
    for phone in (a, b):
        await phone.open()
        await phone.join()
    assert a.participant_id != b.participant_id
    assert ID_PATTERN.match(a.participant_id) and ID_PATTERN.match(b.participant_id)
    assert a.reconnects == b.reconnects == 0


async def test_identity_is_scoped_to_the_meeting_path(server, phones):
    other_url = f"ws://{server.host}:{server.port}/ws/OTHER-MEETING"
    a, b = phones(), phones(url=other_url)
    for phone in (a, b):
        await phone.open()
        await phone.join()
    assert a.participant_id != b.participant_id
    assert {key[0] for key in prototype.participants} == {"TEST-123", "OTHER-MEETING"}


async def test_any_meeting_path_is_accepted_without_validation(server, phones):
    phone = phones(url=f"ws://{server.host}:{server.port}/ws/never-created")
    await phone.open()
    assert (await phone.join())["type"] == "joined"


async def test_duplicate_join_on_one_socket_counts_as_reconnect_and_keeps_socket(phones):
    phone = phones()
    await phone.open()
    await phone.join()
    again = await phone.join()
    assert again["reconnects"] == 1
    assert not phone._ws.closed


# --- metrics ------------------------------------------------------------------------------


async def test_metrics_empty_before_any_join(server):
    assert await get_metrics(server) == []


async def test_metrics_shape_for_participant_without_a_peer(server, phones):
    phone = phones()
    await phone.open()
    await phone.join()
    (row,) = await get_metrics(server)
    assert row == {
        "participant": phone.participant_id,
        "meeting": "TEST-123",
        "state": "disconnected",
        "audioReceived": False,
        "lastAudioAgeMs": None,
        "audioDurationSeconds": 0.0,
        "reconnects": 0,
    }


async def test_metrics_report_no_audio_gap_field_yet(server, phones):
    """docs/transport.md requires audio gaps; the prototype does not measure them (CON-04 adds)."""
    phone = phones()
    await phone.open()
    await phone.join()
    (row,) = await get_metrics(server)
    assert not any("gap" in key.lower() for key in row)


# --- cleanup on socket close --------------------------------------------------------------


async def test_closing_the_socket_detaches_it_but_keeps_the_participant_record(server, phones):
    phone = phones()
    await phone.open()
    await phone.join()
    participant = prototype.participants[("TEST-123", TOKEN)]
    assert participant["ws"] is not None

    await phone.drop_signaling()
    for _ in range(100):
        if participant["ws"] is None:
            break
        await asyncio.sleep(0.02)

    assert participant["ws"] is None
    # Never evicted: state lives in a module-level dict for the process lifetime (gap: CON-03/04).
    assert ("TEST-123", TOKEN) in prototype.participants
    (row,) = await get_metrics(server)
    assert row["state"] == "disconnected"
    assert row["reconnects"] == 0


async def test_rejoin_after_socket_close_keeps_id_and_counts_reconnect(phones):
    first = phones()
    await first.open()
    await first.join()
    await first.drop_signaling()
    await asyncio.sleep(0.2)
    second = phones()
    await second.open()
    rejoined = await second.join()
    assert rejoined["participantId"] == first.participant_id
    assert rejoined["reconnects"] == 1
