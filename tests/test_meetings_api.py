"""REST API and served pages against the real app (docs/api.md). No phones, certificates, or models."""
import re
from pathlib import Path

import pytest
from aiohttp import ClientSession

from server.repositories import audit_events, devices, meetings
from server.ids import new_id
from tests.support.server import settings_in, start_server
from tests.test_api_contract_docs import DERIVED, ENTITY_FIELDS

ROOT = Path(__file__).resolve().parents[1]
UUID = "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18"


@pytest.fixture
async def http():
    async with ClientSession() as session:
        yield session


async def create(http, server, **body):
    async with http.post(server.base_url + "/api/meetings", json=body) as response:
        return response.status, await response.json()


async def make_meeting(http, server) -> str:
    return (await create(http, server))[1]["meeting"]["meeting_id"]


def register_body(**over):
    return {"device_id": new_id(), "display_name": "Priya", "is_shared": False, **over}


async def register(http, server, meeting_id, **over):
    async with http.post(f"{server.base_url}/api/meetings/{meeting_id}/devices", json=register_body(**over)) as r:
        return r.status, await r.json()


def assert_error(body, code):
    assert set(body) == {"error"} and set(body["error"]) == {"code", "message"}
    assert body["error"]["code"] == code and body["error"]["message"]


def assert_entity(row, entity, extra=()):
    """A response row uses exactly the stored fields plus the documented derived fields."""
    assert set(row) == ENTITY_FIELDS[entity] | set(extra), entity


# --- POST /api/meetings ---------------------------------------------------------------------


async def test_create_meeting_returns_the_documented_body(http, server):
    status, body = await create(http, server, title="Sprint planning")
    assert status == 201
    assert set(body) == {"meeting", "join_url", "qr_svg", "warnings"}
    meeting = body["meeting"]
    assert_entity(meeting, "Meeting")
    assert (meeting["title"], meeting["status"], meeting["started_at"], meeting["ended_at"]) == ("Sprint planning", "created", None, None)
    assert body["join_url"] == f"https://192.168.50.10:{server.port}/join/{meeting['meeting_id']}"
    assert "<svg" in body["qr_svg"] and body["warnings"] == []


async def test_default_title_and_audit_event(http, server):
    status, body = await create(http, server)
    assert status == 201 and body["meeting"]["title"].startswith("Meeting 20")
    events = audit_events.list_events(server.runtime.db.conn, event_type="meeting_created")
    assert [e.meeting_id for e in events] == [body["meeting"]["meeting_id"]]


async def test_every_create_makes_a_new_meeting(http, server):
    assert await make_meeting(http, server) != await make_meeting(http, server)


async def test_no_lan_address_still_creates_the_meeting_with_null_join_fields(tmp_path):
    server = await start_server(settings_in(tmp_path), host=None)
    try:
        async with ClientSession() as http:
            status, body = await create(http, server)
        assert status == 201 and body["join_url"] is None and body["qr_svg"] is None
        assert body["warnings"] == ["no_lan_address"]
    finally:
        await server.stop()


@pytest.mark.parametrize("title", [123, ["x"], "x" * 201])
async def test_invalid_titles_are_rejected(http, server, title):
    status, body = await create(http, server, title=title)
    assert status == 400
    assert_error(body, "invalid_request")


async def test_body_rules_apply_to_every_post(http, server):
    url = server.base_url + "/api/meetings"
    async with http.post(url) as response:  # no body at all is `{}`
        assert response.status == 201
    async with http.post(url, data="title=x", headers={"Content-Type": "text/plain"}) as response:
        assert response.status == 415
        assert_error(await response.json(), "unsupported_media_type")
    async with http.post(url, data="{not json", headers={"Content-Type": "application/json; charset=utf-8"}) as response:
        assert response.status == 400
        assert_error(await response.json(), "invalid_request")
    async with http.post(url, json=["a", "list"]) as response:
        assert response.status == 400
        assert_error(await response.json(), "invalid_request")
    async with http.post(url, json={"title": "x" * 70_000}) as response:
        assert response.status == 413
        assert_error(await response.json(), "payload_too_large")
    async def chunked():  # an async generator body is sent chunked, so there is no Content-Length to check
        yield b'{"title":"'
        for _ in range(70):
            yield b"x" * 1000
        yield b'"}'

    async with http.post(url, data=chunked(), headers={"Content-Type": "application/json"}) as response:
        assert response.status == 413
        assert_error(await response.json(), "payload_too_large")


# --- GET /api/meetings/{id} -----------------------------------------------------------------


async def test_get_meeting_shape_and_snapshot_cursor(http, server):
    meeting_id = await make_meeting(http, server)
    async with http.get(f"{server.base_url}/api/meetings/{meeting_id}") as response:
        body = await response.json()
        assert response.status == 200
    assert set(body) == {"meeting", "devices", "join_url", "qr_svg", "latest_summary", "latest_export",
                         "summary_pending", "as_of_seq"}
    assert body["devices"] == [] and body["latest_summary"] is None and body["summary_pending"] is False
    assert body["join_url"].endswith(f"/join/{meeting_id}") and "<svg" in body["qr_svg"]
    assert body["as_of_seq"] == audit_events.max_seq(server.runtime.db.conn) >= 1


async def test_get_meeting_lists_devices_with_participants_and_null_gauges(http, server):
    meeting_id = await make_meeting(http, server)
    _, registered = await register(http, server, meeting_id, display_name="Priya")
    async with http.get(f"{server.base_url}/api/meetings/{meeting_id}") as response:
        (device,) = (await response.json())["devices"]
    assert_entity(device, "Device", DERIVED["Device"])
    assert device["device_id"] == registered["device"]["device_id"] and device["status"] == "joining"
    (person,) = device["participants"]
    assert_entity(person, "Participant")
    assert person["display_name"] == "Priya" and person["enrollment_status"] == "not_required"
    assert device["gauges"] == {"last_audio_age_ms": None, "audio_duration_s": None, "stt_backlog": None,
                                "stt_dropped_windows": None}


@pytest.mark.parametrize("path,status,code", [
    (f"/api/meetings/{UUID}", 404, "meeting_not_found"),
    ("/api/meetings/not-a-uuid", 400, "invalid_request"),
])
async def test_get_meeting_errors(http, server, path, status, code):
    async with http.get(server.base_url + path) as response:
        assert response.status == status
        assert_error(await response.json(), code)


# --- POST /api/meetings/{id}/devices --------------------------------------------------------


async def test_register_device_creates_one_participant_and_returns_the_documented_body(http, server):
    meeting_id = await make_meeting(http, server)
    body = register_body(display_name="  Priya ")
    async with http.post(f"{server.base_url}/api/meetings/{meeting_id}/devices", json=body,
                         headers={"User-Agent": "Mozilla/5.0 test"}) as response:
        assert response.status == 201
        payload = await response.json()
    assert set(payload) == {"device"}
    device = payload["device"]
    assert_entity(device, "Device", ["participants"])
    assert (device["status"], device["is_shared"], device["declared_speaker_count"], device["reconnect_count"],
            device["user_agent"], device["meeting_id"]) == ("joining", False, 1, 0, "Mozilla/5.0 test", meeting_id)
    (person,) = device["participants"]
    assert person["display_name"] == "Priya" and person["device_id"] == body["device_id"]
    assert meetings.get(server.runtime.db.conn, meeting_id).status == "created"  # registering does not start it
    assert [e.event_type for e in audit_events.list_events(server.runtime.db.conn, event_type="device_registered")] == ["device_registered"]


async def test_registering_again_is_an_idempotent_replay(http, server):
    meeting_id = await make_meeting(http, server)
    body = register_body()
    async with http.post(f"{server.base_url}/api/meetings/{meeting_id}/devices", json=body) as r:
        first = await r.json()
    async with http.post(f"{server.base_url}/api/meetings/{meeting_id}/devices", json={**body, "display_name": "Other"}) as r:
        assert r.status == 200
        again = await r.json()
    assert again == first  # same device, same participant, same name in force
    assert len(audit_events.list_events(server.runtime.db.conn, event_type="device_registered")) == 1


@pytest.mark.parametrize("over,code", [
    ({"device_id": "nope"}, "invalid_request"), ({"device_id": 5}, "invalid_request"),
    ({"display_name": ""}, "invalid_request"), ({"display_name": None}, "invalid_request"),
    ({"display_name": "x" * 81}, "invalid_request"),
    ({"is_shared": "yes"}, "invalid_request"), ({"declared_speaker_count": "2"}, "invalid_request"),
    ({"declared_speaker_count": 2}, "invalid_request"),
    ({"is_shared": True, "declared_speaker_count": 5}, "invalid_request"),
])
async def test_register_validation(http, server, over, code):
    meeting_id = await make_meeting(http, server)
    status, body = await register(http, server, meeting_id, **over)
    assert status == 400
    assert_error(body, code)
    assert devices.list_for_meeting(server.runtime.db.conn, meeting_id) == []


async def test_register_error_codes(http, server):
    meeting_id, other = await make_meeting(http, server), await make_meeting(http, server)
    status, body = await register(http, server, UUID)
    assert status == 404
    assert_error(body, "meeting_not_found")
    _, first = await register(http, server, meeting_id)
    status, body = await register(http, server, other, device_id=first["device"]["device_id"])
    assert status == 409
    assert_error(body, "device_conflict")
    async with http.post(f"{server.base_url}/api/meetings/{meeting_id}/end") as r:
        assert r.status == 202
    status, body = await register(http, server, meeting_id)
    assert status == 409
    assert_error(body, "meeting_ended")


async def test_a_shared_device_registers_as_enrolling_without_participants(http, server):
    meeting_id = await make_meeting(http, server)
    status, body = await register(http, server, meeting_id, is_shared=True, declared_speaker_count=2, display_name=None)
    assert status == 201
    assert (body["device"]["status"], body["device"]["participants"]) == ("enrolling", [])


# --- POST /api/meetings/{id}/end ------------------------------------------------------------


async def test_end_is_idempotent_and_persists_the_meeting_and_devices(http, server):
    meeting_id = await make_meeting(http, server)
    _, registered = await register(http, server, meeting_id)
    async with http.post(f"{server.base_url}/api/meetings/{meeting_id}/end") as response:
        assert response.status == 202
        first = await response.json()
    assert set(first) == {"meeting", "summary_pending"} and first["summary_pending"] is False
    assert first["meeting"]["status"] == "ended" and first["meeting"]["ended_at"]
    async with http.post(f"{server.base_url}/api/meetings/{meeting_id}/end") as response:
        assert response.status == 200  # already ended, nothing further happens
        assert (await response.json())["meeting"] == first["meeting"]
    conn = server.runtime.db.conn
    assert devices.get(conn, registered["device"]["device_id"]).status == "left"
    assert len(audit_events.list_events(conn, event_type="meeting_ended")) == 1
    assert len(audit_events.list_events(conn, event_type="device_left")) == 1


async def test_ended_meeting_detail_has_no_join_link(http, server):
    meeting_id = await make_meeting(http, server)
    async with http.post(f"{server.base_url}/api/meetings/{meeting_id}/end"):
        pass
    async with http.get(f"{server.base_url}/api/meetings/{meeting_id}") as response:
        body = await response.json()
    assert body["meeting"]["status"] == "ended" and body["join_url"] is None and body["qr_svg"] is None


async def test_end_errors(http, server):
    async with http.post(f"{server.base_url}/api/meetings/{UUID}/end") as response:
        assert response.status == 404
        assert_error(await response.json(), "meeting_not_found")
    async with http.post(f"{server.base_url}/api/meetings/bad/end") as response:
        assert response.status == 400


# --- pages and routing ----------------------------------------------------------------------


async def test_pages_are_served_locally(http, server):
    meeting_id = await make_meeting(http, server)
    base = server.base_url
    for path in ("/", f"/join/{meeting_id}", f"/dashboard/{meeting_id}", "/static/app.js", "/static/dashboard.js"):
        async with http.get(base + path) as response:
            assert response.status == 200, path
    async with http.get(f"{base}/join/{meeting_id}") as response:
        text = await response.text()
    assert "audio is being transcribed" in text and 'id="name"' in text  # consent line and name input


async def test_unknown_meeting_pages_are_a_plain_404_page(http, server):
    for path in (f"/join/{UUID}", f"/dashboard/{UUID}", "/join/not-a-uuid"):
        async with http.get(server.base_url + path) as response:
            assert response.status == 404 and "Meeting not found" in await response.text()


async def test_dashboard_of_an_ended_meeting_redirects_to_the_post_meeting_view(http, server):
    meeting_id = await make_meeting(http, server)
    async with http.post(f"{server.base_url}/api/meetings/{meeting_id}/end"):
        pass
    async with http.get(f"{server.base_url}/dashboard/{meeting_id}", allow_redirects=False) as response:
        assert response.status == 302 and response.headers["Location"] == f"/meetings/{meeting_id}"


async def test_prototype_routes_and_api_docs_pages_are_gone(http, server):
    for path in ("/app.js", "/docs", "/redoc", "/openapi.json"):  # the docs pages would load assets from a CDN
        async with http.get(server.base_url + path) as response:
            assert response.status == 404, path
            assert_error(await response.json(), "not_found")


def test_no_page_loads_anything_from_another_origin():
    """Local-first: no CDN, no external fonts, no analytics."""
    external = re.compile(r"""(?:src|href)\s*=\s*["']?\s*(?:https?:)?//|@import|url\(\s*["']?https?:|fetch\(\s*["']https?:""", re.I)
    checked = 0
    for path in (ROOT / "client").iterdir():
        if path.suffix in (".html", ".js", ".css"):
            checked += 1
            assert not external.search(path.read_text()), f"{path.name} loads something from another origin"
    assert checked >= 4


def test_the_aiohttp_prototype_server_is_gone():
    importing = re.compile(r"^\s*(?:import|from)\s+aiohttp\b", re.M)
    for path in (ROOT / "server").rglob("*.py"):
        assert not importing.search(path.read_text()), f"{path.relative_to(ROOT)} still imports aiohttp"
    runtime_requirements = (ROOT / "requirements.txt").read_text().lower()
    assert "aiohttp" not in runtime_requirements and "fastapi" in runtime_requirements and "uvicorn" in runtime_requirements
