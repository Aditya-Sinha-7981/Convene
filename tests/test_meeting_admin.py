"""ADR-27: rename (PATCH) and permanent delete (DELETE /api/meetings/{id}) against the real app."""
import asyncio
from dataclasses import replace

import pytest
from aiohttp import ClientSession

from server.rag.qa import HISTORY_SYSTEM_PROMPT, SYSTEM_PROMPT
from server.rag.reasoning import FakeReasoningAdapter
from server.repositories import audit_events
from tests.support.qa import QA, RAG, TopicEmbedding
from tests.support.server import settings_in, start_server
from tests.support.summary import summary_json
from tests.test_meetings_list import ended_meeting_with_a_line

TABLES = ("Device", "Participant", "Utterance", "ConnectionEvent", "AuditEvent", "TranscriptChunk", "QAQuery",
          "Summary", "ActionItem", "Export")


def reply(messages):
    if messages[0]["content"] in (SYSTEM_PROMPT, HISTORY_SYSTEM_PROMPT):
        return "Priya said it."
    return summary_json("The team talked.")


@pytest.fixture
async def server(tmp_path):
    settings = replace(settings_in(tmp_path), rag=RAG, qa=QA)
    running = await start_server(settings, embedding_adapter=TopicEmbedding(), reasoning_adapter=FakeReasoningAdapter(reply))
    yield running
    await running.stop()


@pytest.fixture
async def http():
    async with ClientSession() as session:
        yield session


async def exported(http, server, meeting_id):
    """Wait for the summary and the automatic DOCX after meeting end."""
    for _ in range(200):
        async with http.get(server.base_url + f"/api/meetings/{meeting_id}/export/status") as response:
            body = await response.json()
        if body.get("export"):
            return body
        await asyncio.sleep(.03)
    raise AssertionError("no export was rendered")


def rows(db, table, meeting_id):
    return db.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE meeting_id = ?", (meeting_id,)).fetchone()[0]


async def ask(http, server, question, meeting_ids):
    async with http.post(server.base_url + "/api/qa", json={"question": question, "meeting_ids": meeting_ids}) as r:
        return (await r.json())


# --- rename -----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_rename_updates_the_title_audits_once_and_marks_the_docx_stale(server, http):
    meeting_id = await ended_meeting_with_a_line(http, server, "The budget is forty thousand.")
    assert (await exported(http, server, meeting_id))["stale"] is False
    url = server.base_url + f"/api/meetings/{meeting_id}"
    for _ in range(2):
        async with http.patch(url, json={"title": "  Budget   review "}) as response:
            assert response.status == 200
            assert (await response.json())["meeting"]["title"] == "Budget review"
    events = audit_events.list_events(server.runtime.db.conn, meeting_id=meeting_id, event_type="meeting_renamed")
    assert [event.payload for event in events] == [{"title": "Budget review"}]
    async with http.get(url + "/export/status") as response:
        assert (await response.json())["stale"] is True
    async with http.get(server.base_url + "/api/meetings?q=budget%20review") as response:
        assert [m["meeting_id"] for m in (await response.json())["meetings"]] == [meeting_id]


@pytest.mark.asyncio
@pytest.mark.parametrize("body", [{}, {"title": ""}, {"title": "   "}, {"title": 5}, {"title": "x" * 201},
                                  {"title": "ok", "status": "ended"}])
async def test_bad_rename_bodies_are_invalid_request(server, http, body):
    async with http.post(server.base_url + "/api/meetings", json={}) as response:
        meeting_id = (await response.json())["meeting"]["meeting_id"]
    async with http.patch(server.base_url + f"/api/meetings/{meeting_id}", json=body) as response:
        assert response.status == 400 and (await response.json())["error"]["code"] == "invalid_request"


# --- delete -----------------------------------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_delete_erases_everything_the_meeting_owns_and_nothing_else(server, http):
    db = server.runtime.db
    doomed = await ended_meeting_with_a_line(http, server, "The budget is forty thousand.")
    kept = await ended_meeting_with_a_line(http, server, "The hotel is near the airport.")
    await exported(http, server, doomed)
    await exported(http, server, kept)
    file = server.runtime.settings.exports_dir / f"{doomed}.docx"
    assert file.is_file() and (server.runtime.settings.exports_dir / f"{kept}.docx").is_file()
    cited_doomed = await ask(http, server, "What is the budget?", [doomed, kept])
    cited_kept = await ask(http, server, "Where is the hotel?", [doomed, kept])
    assert cited_doomed["citations"][0]["meeting_id"] == doomed and cited_kept["citations"][0]["meeting_id"] == kept
    before = audit_events.max_seq(db.conn)

    async with http.delete(server.base_url + f"/api/meetings/{doomed}") as response:
        assert response.status == 200
        body = await response.json()
    assert body["deleted"]["meeting_id"] == doomed and body["deleted"]["utterance_count"] == 1
    assert body["deleted"]["qa_query_count"] == 1  # the multi-meeting answer that cited it

    for table in (*TABLES, "Meeting"):
        assert rows(db, table, doomed) == 0, table
    assert db.conn.execute("SELECT COUNT(*) FROM TranscriptChunkVector WHERE meeting_id = ?", (doomed,)).fetchone()[0] == 0
    assert not file.exists()
    remaining = {row[0] for row in db.conn.execute("SELECT query_id FROM QAQuery").fetchall()}
    assert cited_doomed["query"]["query_id"] not in remaining and cited_kept["query"]["query_id"] in remaining
    [event] = audit_events.list_events(db.conn, event_type="meeting_deleted")
    assert event.meeting_id is None and event.payload["deleted_meeting_id"] == doomed and event.seq > before
    assert audit_events.max_seq(db.conn) >= before  # seq never goes backwards, so resync cursors stay valid
    for table in ("Meeting", "Device", "Utterance", "TranscriptChunk", "Summary", "Export", "AuditEvent"):
        assert rows(db, table, kept) > 0, table  # the other meeting is untouched
    assert (server.runtime.settings.exports_dir / f"{kept}.docx").is_file()

    async with http.get(server.base_url + f"/api/meetings/{doomed}") as response:
        assert response.status == 404
    async with http.delete(server.base_url + f"/api/meetings/{doomed}") as response:
        assert response.status == 404
    after = await ask(http, server, "Where is the hotel?", None)
    assert after["query"]["status"] == "answered" and [c["meeting_id"] for c in after["citations"]] == [kept]
    # The indexer keeps working (a queued re-index of the deleted meeting is skipped, not an error loop).
    server.runtime.indexer.enqueue_meeting(doomed)
    await asyncio.sleep(RAG.settle_delay_s * 5)


@pytest.mark.asyncio
async def test_delete_is_refused_while_a_phone_is_connected_or_a_summary_is_running(server, http, monkeypatch):
    db = server.runtime.db
    async with http.post(server.base_url + "/api/meetings", json={}) as response:
        meeting_id = (await response.json())["meeting"]["meeting_id"]
    async with http.post(server.base_url + f"/api/meetings/{meeting_id}/devices",
                         json={"device_id": "7f3a9c52-1e84-4d6b-a0b7-3c5d9e2f4a18", "display_name": "Priya"}):
        pass
    with db.transaction() as tx:
        tx.conn.execute("UPDATE Device SET status = 'connected' WHERE meeting_id = ?", (meeting_id,))
        tx.conn.execute("UPDATE Meeting SET status = 'live' WHERE meeting_id = ?", (meeting_id,))
    url = server.base_url + f"/api/meetings/{meeting_id}"
    async with http.delete(url) as response:
        assert response.status == 409 and (await response.json())["error"]["code"] == "meeting_active"

    with db.transaction() as tx:  # abandoned: the phone is gone, the meeting is still marked live
        tx.conn.execute("UPDATE Device SET status = 'disconnected' WHERE meeting_id = ?", (meeting_id,))
    monkeypatch.setattr(server.runtime.summary, "running", lambda mid: "some-summary")
    async with http.delete(url) as response:
        assert response.status == 409 and (await response.json())["error"]["code"] == "summary_in_progress"
    monkeypatch.undo()
    async with http.delete(url) as response:
        assert response.status == 200
    assert rows(db, "Meeting", meeting_id) == 0
