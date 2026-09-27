"""CON-16: the global action-item list (open by default, sort, filters, pagination, current summaries only)."""
import asyncio

import pytest
from aiohttp import ClientSession

from server.action_items import service
from server.action_items.views import list_items
from server.ids import new_id
from server.repositories import summaries
from server.timeutil import utc_now
from tests.support.action_items import add_summary, seed_meeting

TODAY = "2026-09-27"


def edit(db, item_id, **changes):
    with db.transaction() as tx:
        service.update_action_item(tx, item_id, changes)


def listed(db, **filters):
    body = list_items(db.conn, today=TODAY, **filters)
    return [item["text"] for item in body["action_items"]], body["total"]


def test_default_is_open_items_only_and_closed_items_never_push_them_off_the_first_page(db):
    busy = seed_meeting(db, title="Busy", items=[(f"closed {n}", None) for n in range(60)])
    for n, item_id in enumerate(busy.items):
        edit(db, item_id, status="done" if n % 2 else "cancelled")
    seed_meeting(db, title="Quiet", items=[("the one open item", "Sam")])
    texts, total = listed(db, limit=50)
    assert texts == ["the one open item"] and total == 1
    assert listed(db, statuses=("done",), limit=200)[1] == 30
    assert listed(db, statuses=("done", "cancelled"), limit=200)[1] == 60
    assert listed(db, statuses=("open", "done", "cancelled"), limit=10)[1] == 61


def test_due_sort_puts_no_date_last_and_breaks_ties_by_recency(db):
    seeded = seed_meeting(db, items=[("no date", None), ("late", None), ("early", None), ("early too", None)])
    none, late, early, early_too = seeded.items
    edit(db, late, due_date="2026-10-05")
    edit(db, early, due_date="2026-10-01")
    edit(db, early_too, due_date="2026-10-01")  # changed after "early", so it is more recent
    assert listed(db)[0] == ["early too", "early", "late", "no date"]
    edit(db, none, owner_participant_id=seeded.people["Priya"])
    assert listed(db, sort="recent")[0] == ["no date", "early too", "early", "late"]


def test_recent_sort_counts_notes_and_falls_back_to_the_summary(db):
    old = seed_meeting(db, title="Old", items=[("old item", None)])
    new = seed_meeting(db, title="New", items=[("new item", None)])
    assert listed(db, sort="recent")[0] == ["new item", "old item"]
    with db.transaction() as tx:
        service.add_action_item_note(tx, old.items[0], new.meeting_id, "Mentioned again.")
    items = list_items(db.conn, sort="recent", today=TODAY)["action_items"]
    assert [i["text"] for i in items] == ["old item", "new item"]
    assert [(i["last_changed_by"], i["note_count"]) for i in items] == [("manual", 1), ("summary", 0)]


def test_filters_and_pagination(db):
    first = seed_meeting(db, title="First", people=("Priya", "Sam"),
                         items=[("a", "Priya"), ("b", "Sam"), ("c", None), ("d", "Priya")])
    second = seed_meeting(db, title="Second", people=("priya ", "Ana"), items=[("e", "priya "), ("f", "Ana")])
    edit(db, first.items[0], due_date="2026-09-01")      # overdue
    edit(db, first.items[1], due_date="2026-09-30")
    edit(db, first.items[3], due_date="2026-09-02", status="done")  # past due but closed: not overdue
    edit(db, second.items[0], due_date="2026-10-10")

    assert listed(db, owner="PRIYA")[0] == ["a", "e"]  # one person across meetings, by name
    assert listed(db, owner="nobody") == ([], 0)
    assert listed(db, meeting_id=second.meeting_id)[0] == ["e", "f"]
    assert listed(db, meeting_id=new_id()) == ([], 0)
    assert listed(db, due_after="2026-09-02", due_before="2026-09-30")[0] == ["b"]
    assert listed(db, due_after="2026-09-30")[0] == ["b", "e"]
    assert listed(db, overdue=True)[0] == ["a"]
    items = {i["text"]: i["overdue"] for i in list_items(db.conn, statuses=("open", "done"), today=TODAY)["action_items"]}
    assert items == {"a": True, "b": False, "c": False, "d": False, "e": False, "f": False}
    page_one, total = listed(db, limit=2)
    page_two, _ = listed(db, limit=2, offset=2)
    page_three, _ = listed(db, limit=2, offset=4)
    assert total == 5 and page_one + page_two + page_three == ["a", "b", "e", "f", "c"]  # no due date: newest summary first


def test_only_the_current_summary_is_listed(db):
    seeded = seed_meeting(db, items=[("from the first summary", None)])
    edit(db, seeded.items[0], status="done")
    with db.transaction() as tx:
        add_summary(tx, seeded.meeting_id, [("from the regenerated summary", None)], seeded.people)
        failed = new_id()  # a newer failed attempt never replaces the current one
        summaries.insert_pending(tx.conn, summary_id=failed, meeting_id=seeded.meeting_id, model_identifier="fake")
        summaries.finish(tx.conn, failed, status="failed", generated_at=utc_now(), error_message="boom")
    assert listed(db, statuses=("open", "done", "cancelled")) == (["from the regenerated summary"], 1)


def test_meeting_fields_and_owner_names(db):
    seeded = seed_meeting(db, title="Sprint planning")
    [item] = list_items(db.conn, today=TODAY)["action_items"]
    meeting = db.conn.execute("SELECT * FROM Meeting WHERE meeting_id = ?", (seeded.meeting_id,)).fetchone()
    assert item["meeting_title"] == "Sprint planning" and item["meeting_id"] == seeded.meeting_id
    assert item["meeting_started_at"] == (meeting["started_at"] or meeting["created_at"])
    assert item["owner_display_name"] == "Sam" and item["due_date"] is None


# --- HTTP -----------------------------------------------------------------------------------------------------------


@pytest.fixture
async def http():
    async with ClientSession() as session:
        yield session


@pytest.mark.asyncio
async def test_list_route(server, http):
    db = server.runtime.db
    seeded = seed_meeting(db, items=[("open one", "Sam"), ("done one", None)])
    edit(db, seeded.items[1], status="done")
    async with http.get(server.base_url + "/api/action-items") as response:
        body = await response.json()
    assert response.status == 200 and body["total"] == 1 and body["action_items"][0]["text"] == "open one"
    async with http.get(server.base_url + "/api/action-items?status=all&sort=recent&limit=1") as response:
        body = await response.json()
    assert body["total"] == 2 and [i["text"] for i in body["action_items"]] == ["done one"]
    async with http.get(server.base_url + "/api/action-items?status=open,done&owner=%20sam%20") as response:
        assert [i["text"] for i in (await response.json())["action_items"]] == ["open one"]


@pytest.mark.asyncio
@pytest.mark.parametrize("query", ["status=archived", "status=open,", "sort=owner", "limit=0", "limit=201",
                                   "offset=-1", "due_after=2026-9-1", "due_before=tomorrow", "overdue=yes",
                                   "meeting_id=nope", "owner=" + "x" * 81])
async def test_bad_list_parameters_are_invalid_request(server, http, query):
    async with http.get(server.base_url + f"/api/action-items?{query}") as response:
        assert response.status == 400 and (await response.json())["error"]["code"] == "invalid_request"


@pytest.mark.asyncio
async def test_pages_are_served_and_linked(server, http):
    async with http.get(server.base_url + "/action-items") as response:
        assert response.status == 200 and "action_items.js" in await response.text()
    for page in ("/", "/history"):
        async with http.get(server.base_url + page) as response:
            assert 'href="/action-items"' in await response.text(), page


@pytest.mark.asyncio
async def test_editing_does_not_touch_a_live_meetings_feed(server, http):
    """Device isolation: an edit reads and writes stored rows only; nothing reaches another meeting's dashboard."""
    seeded = seed_meeting(server.runtime.db)
    live = seed_meeting(server.runtime.db, title="Live", items=[], ended=False)
    async with http.get(server.base_url + f"/api/meetings/{live.meeting_id}/transcript") as response:
        before = (await response.json())["utterances"]
    ws = await http.ws_connect(server.ws_url(f"/ws/dashboard/{live.meeting_id}"))
    try:
        async with http.patch(server.base_url + f"/api/action-items/{seeded.items[0]}",
                              json={"status": "done", "due_date": "2026-10-01"}) as response:
            assert response.status == 200
        async with http.post(server.base_url + f"/api/action-items/{seeded.items[0]}/notes",
                             json={"source_meeting_id": live.meeting_id, "text": "Mentioned live."}) as response:
            assert response.status == 201
        deadline = asyncio.get_running_loop().time() + 0.5
        while (left := deadline - asyncio.get_running_loop().time()) > 0:
            try:
                message = await ws.receive_json(timeout=left)
            except asyncio.TimeoutError:
                break
            assert message["type"] == "device_gauges", message
    finally:
        await ws.close()
    async with http.get(server.base_url + f"/api/meetings/{live.meeting_id}/transcript") as response:
        assert (await response.json())["utterances"] == before
