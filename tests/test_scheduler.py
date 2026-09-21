"""The STT scheduler: fairness, bounded queues and the overload policy, failure isolation, worker recovery, drain,
and the compute-priority hook. All with the fake adapter, so no model weights are needed."""
import asyncio
import threading
import time

import numpy as np
import pytest

from server.audit import emit
from server.db import Database
from server.pipeline.adapter import FakeAdapter
from server.pipeline.priority import ComputePriority
from server.pipeline.scheduler import SttScheduler
from server.pipeline.windowing import Window
from server.registry import create_meeting
from server.repositories import audit_events, model_executions
from tests.support.util import wait_for

A, B, C = "aaaaaaaa-0000-4000-8000-000000000001", "bbbbbbbb-0000-4000-8000-000000000002", "cccccccc-0000-4000-8000-000000000003"


def window(device: str, n: int, meeting: str | None = "m1", speech_fraction: float = 1.0, strength_db: float = 99.0) -> Window:
    return Window(device, meeting, n, np.zeros(16000, np.float32), 16000, "2026-09-21T11:30:00.000Z",
                  "2026-09-21T11:30:01.000Z", speech_fraction, time.monotonic(), strength_db)


class Collector:
    def __init__(self):
        self.outcomes = []

    def __call__(self, outcome):
        self.outcomes.append(outcome)

    def of(self, device, status=None):
        return [o for o in self.outcomes if o.device_id == device and (status is None or o.status == status)]


@pytest.fixture
async def make():
    made = []

    async def build(adapter=None, workers=1, queue_max=5, timeout=10.0, db=None, **kw):
        collector = Collector()
        scheduler = SttScheduler(adapter or FakeAdapter(), workers=workers, queue_max=queue_max, window_timeout_s=timeout,
                                 db=db, on_window=collector, **kw)
        scheduler.start()
        made.append(scheduler)
        return scheduler, collector

    yield build
    for scheduler in made:
        await scheduler.stop()


# --- fairness -------------------------------------------------------------------------------


async def test_a_flooding_device_does_not_starve_a_quiet_one(make):
    """Round robin: after the flood is queued, B's single window is served within one rotation, not after 40 of A's."""
    adapter = FakeAdapter(delay_s=0.01)
    scheduler, got = await make(adapter, queue_max=50)
    for n in range(1, 41):
        scheduler.submit(window(A, n))
    await asyncio.sleep(0.03)  # the worker is busy with A
    scheduler.submit(window(B, 1))
    await wait_for(lambda: got.of(B, "ok"), timeout=5)
    a_done_before_b = [o for o in got.outcomes[:got.outcomes.index(got.of(B)[0])] if o.device_id == A]
    assert len(a_done_before_b) <= 4, f"B waited behind {len(a_done_before_b)} of A's windows"
    await scheduler.drain("m1", 10)
    assert len(got.of(A, "ok")) == 40


async def test_three_devices_are_served_in_rotation(make):
    adapter = FakeAdapter(delay_s=0.005)
    scheduler, got = await make(adapter, queue_max=20)
    for n in range(1, 7):
        for device in (A, B, C):
            scheduler.submit(window(device, n))
    await scheduler.drain("m1", 10)
    order = [o.device_id for o in got.outcomes]
    for i in range(0, 18 - 2, 3):  # every consecutive triple contains each device once
        assert set(order[i:i + 3]) == {A, B, C}, order


# --- bounded queue, overload policy, counters ------------------------------------------------


async def test_a_full_queue_drops_the_oldest_window_and_counts_and_audits_every_drop(make, db):
    with db.transaction() as tx:
        meeting = create_meeting(tx, "m")
    gate = threading.Event()
    adapter = FakeAdapter(delay_s=lambda d, w: gate.wait(5) and 0)  # the worker blocks until released
    scheduler, got = await make(adapter, queue_max=3, db=db)
    scheduler.submit(window(A, 1, meeting.meeting_id))
    await wait_for(lambda: scheduler.devices[A].in_flight == 1, message="the worker must take window 1")
    for n in range(2, 11):
        scheduler.submit(window(A, n, meeting.meeting_id))
    await wait_for(lambda: scheduler.devices[A].dropped >= 6, message="six windows must be dropped")
    await asyncio.sleep(0.1)
    dropped = sorted(o.window_id for o in got.of(A, "dropped"))
    gate.set()
    await scheduler.drain(meeting.meeting_id, 10)

    # window 1 was already in flight; 2..7 were the oldest queued and lost their place to 8, 9, 10
    assert dropped == [2, 3, 4, 5, 6, 7]
    assert sorted(o.window_id for o in got.of(A, "ok")) == [1, 8, 9, 10]
    stats = scheduler.stats()[A]
    assert stats["dropped"] == 6 and stats["enqueued"] == 10
    assert stats["transcribed"] + stats["empty"] + stats["failed"] + stats["dropped"] == stats["enqueued"]  # nothing vanishes
    events = audit_events.list_events(db.conn, event_type="stt_window_dropped")
    assert sorted(e.payload["window_id"] for e in events) == dropped
    assert all(e.payload == {"device_id": A, "window_id": e.payload["window_id"], "reason": "overload"} for e in events)
    assert all(e.meeting_id == meeting.meeting_id for e in events)
    assert all(o.error and "dropped" in o.error for o in got.of(A, "dropped"))


async def test_overload_on_one_device_never_drops_another_devices_windows(make):
    gate = threading.Event()
    adapter = FakeAdapter(delay_s=lambda d, w: gate.wait(5) and 0)
    scheduler, got = await make(adapter, queue_max=2)
    for n in range(1, 9):
        scheduler.submit(window(A, n))
    scheduler.submit(window(B, 1))
    scheduler.submit(window(B, 2))
    gate.set()
    await scheduler.drain("m1", 10)
    assert scheduler.stats()[B]["dropped"] == 0 and len(got.of(B, "ok")) == 2
    assert scheduler.stats()[A]["dropped"] > 0


async def test_backlog_reports_depth_and_oldest_age_per_device(make):
    gate = threading.Event()
    scheduler, _ = await make(FakeAdapter(delay_s=lambda d, w: gate.wait(5) and 0), queue_max=10)
    assert scheduler.backlog(A) == {"depth": 0, "oldest_age_s": 0.0, "dropped": 0}
    for n in range(1, 5):
        scheduler.submit(window(A, n))
    await asyncio.sleep(0.15)
    backlog = scheduler.backlog(A)
    assert backlog["depth"] == 4 and backlog["oldest_age_s"] >= 0.1 and scheduler.total_backlog() == 4
    assert scheduler.backlog(B)["depth"] == 0
    gate.set()
    await scheduler.drain("m1", 10)
    assert scheduler.backlog(A)["depth"] == 0 and scheduler.total_backlog() == 0


async def test_scheduler_arguments_are_validated():
    with pytest.raises(ValueError):
        SttScheduler(FakeAdapter(), workers=0, queue_max=3, window_timeout_s=1)
    with pytest.raises(ValueError):
        SttScheduler(FakeAdapter(), workers=1, queue_max=0, window_timeout_s=1)


# --- failure isolation -----------------------------------------------------------------------


async def test_a_failed_window_is_marked_failed_audited_and_does_not_stop_later_windows(make, db):
    with db.transaction() as tx:
        meeting = create_meeting(tx, "m")
    adapter = FakeAdapter(fail_when=lambda d, w: d == A and w == 2)
    scheduler, got = await make(adapter, db=db)
    for n in range(1, 5):
        scheduler.submit(window(A, n, meeting.meeting_id))
        scheduler.submit(window(B, n, meeting.meeting_id))
    await scheduler.drain(meeting.meeting_id, 10)
    assert [o.window_id for o in got.of(A, "failed")] == [2]
    assert sorted(o.window_id for o in got.of(A, "ok")) == [1, 3, 4]  # A's later windows continue
    assert sorted(o.window_id for o in got.of(B, "ok")) == [1, 2, 3, 4]  # and B is untouched
    failed = got.of(A, "failed")[0]
    assert "injected failure" in failed.error and failed.text == "" and failed.stt_confidence == 0.0
    (event,) = audit_events.list_events(db.conn, event_type="model_error")
    assert event.component == "models" and event.meeting_id == meeting.meeting_id
    assert event.payload["device_id"] == A and event.payload["window_id"] == 2 and event.payload["resource_type"] == "stt"
    assert event.payload["model_identifier"] == "fake/stt" and event.payload["related_id"] == f"{A}/2"
    assert "injected failure" in event.payload["error"]
    assert scheduler.stats()[A]["failed"] == 1 and scheduler.stats()[B]["failed"] == 0
    assert scheduler.worker_restarts == 0  # an adapter failure is an ordinary failed window, not a worker fault


async def test_one_execution_row_is_recorded_per_invocation_with_the_configured_identifier(make, db):
    with db.transaction() as tx:
        meeting = create_meeting(tx, "m")
    scheduler, _ = await make(FakeAdapter(model_identifier="configured/model", fail_when=lambda d, w: w == 2), db=db)
    for n in range(1, 4):
        scheduler.submit(window(A, n, meeting.meeting_id))
    await scheduler.drain(meeting.meeting_id, 10)
    rows = model_executions.list_executions(db.conn, resource_type="stt")
    assert len(rows) == 3 == scheduler.devices[A].enqueued  # failed invocations count too; the audit event says which failed
    assert {r.model_identifier for r in rows} == {"configured/model"} and {r.runtime for r in rows} == {"mlx"}
    assert sorted(r.related_id for r in rows) == [f"{A}/1", f"{A}/2", f"{A}/3"]
    assert all(r.duration_ms >= 0 for r in rows)


async def test_a_window_that_takes_too_long_is_failed_and_the_pool_recovers(make):
    adapter = FakeAdapter(delay_s=lambda d, w: 0.4 if (d == A and w == 1) else 0.0)
    scheduler, got = await make(adapter, workers=2, timeout=0.1)
    scheduler.submit(window(A, 1))
    scheduler.submit(window(A, 2))
    scheduler.submit(window(B, 1))
    await scheduler.drain("m1", 10)
    (timed_out,) = got.of(A, "failed")
    assert timed_out.window_id == 1 and "timed out" in timed_out.error
    assert [o.window_id for o in got.of(A, "ok")] == [2] and len(got.of(B, "ok")) == 1


async def test_a_failing_callback_does_not_stop_the_workers(make):
    seen = []

    def bad(outcome):
        seen.append(outcome.window_id)
        raise RuntimeError("consumer bug")

    scheduler, _ = await make()
    scheduler.on_window = bad
    for n in range(1, 4):
        scheduler.submit(window(A, n))
    await scheduler.drain("m1", 5)
    await wait_for(lambda: len(seen) == 3)
    assert scheduler.stats()[A]["transcribed"] == 3


async def test_an_internal_worker_error_restarts_the_worker_and_reports_the_lost_window(make, monkeypatch):
    scheduler, got = await make()
    real = scheduler._process
    calls = {"n": 0}

    async def flaky(win):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("scheduler bug")
        await real(win)

    monkeypatch.setattr(scheduler, "_process", flaky)
    for n in range(1, 4):
        scheduler.submit(window(A, n))
    await scheduler.drain("m1", 5)
    assert scheduler.worker_restarts == 1
    lost = got.of(A, "failed")
    assert [o.window_id for o in lost] == [1] and "worker error" in lost[0].error  # reported, not silently lost
    assert [o.window_id for o in got.of(A, "ok")] == [2, 3]  # and the worker carried on
    assert scheduler.stats()[A]["failed"] == 1 and scheduler.total_backlog() == 0


async def test_workers_run_concurrently_up_to_the_configured_count(make):
    adapter = FakeAdapter(delay_s=0.05)
    scheduler, _ = await make(adapter, workers=3, queue_max=20)
    for n in range(1, 10):
        scheduler.submit(window(A, n))
        scheduler.submit(window(B, n))
    await scheduler.drain("m1", 10)
    assert adapter.max_concurrent == 3


# --- empty results and hallucination suppression ---------------------------------------------


async def test_a_window_with_no_speech_is_empty_not_failed(make):
    scheduler, got = await make(FakeAdapter(silent_when=lambda d, w: w == 2))
    for n in (1, 2, 3):
        scheduler.submit(window(A, n))
    await scheduler.drain("m1", 5)
    (empty,) = got.of(A, "empty")
    assert empty.window_id == 2 and empty.text == "" and empty.stt_confidence == 0.0 and empty.error is None
    assert scheduler.stats()[A]["empty"] == 1 and scheduler.stats()[A]["failed"] == 0


class Phrase(FakeAdapter):
    def __init__(self, text):
        super().__init__()
        self._text = text

    def transcribe_window(self, device_id, window_id, audio):
        from server.pipeline.adapter import SttResult
        return SttResult(self._text, 0.75)


async def test_a_stock_phrase_on_a_mostly_non_speech_window_is_suppressed_but_real_speech_is_kept(make):
    scheduler, got = await make(Phrase("Thank you."), blocklist=("thank you",), blocklist_max_speech_fraction=0.5)
    scheduler.submit(window(A, 1, speech_fraction=0.25))  # the VAD barely heard anything
    scheduler.submit(window(A, 2, speech_fraction=0.9))   # someone really did say it
    await scheduler.drain("m1", 5)
    assert [o.window_id for o in got.of(A, "empty")] == [1] and scheduler.stats()[A]["suppressed"] == 1
    assert [(o.window_id, o.text) for o in got.of(A, "ok")] == [(2, "Thank you.")]


async def test_a_stock_phrase_on_a_weak_segment_is_suppressed_even_if_it_is_mostly_voiced(make):
    scheduler, got = await make(Phrase("Thank you."), blocklist=("thank you",), blocklist_min_strength_db=10.0)
    scheduler.submit(window(A, 1, speech_fraction=0.95, strength_db=6.0))   # surging noise: voiced, but barely above the floor
    scheduler.submit(window(A, 2, speech_fraction=0.95, strength_db=25.0))  # a clear voice
    await scheduler.drain("m1", 5)
    assert [o.window_id for o in got.of(A, "empty")] == [1] and scheduler.stats()[A]["suppressed"] == 1
    assert [(o.window_id, o.text) for o in got.of(A, "ok")] == [(2, "Thank you.")]


async def test_a_weak_segment_with_other_text_is_never_suppressed(make):
    scheduler, got = await make(Phrase("We ship on Friday."), blocklist=("thank you",), blocklist_min_strength_db=10.0)
    scheduler.submit(window(A, 1, speech_fraction=0.95, strength_db=3.0))
    await scheduler.drain("m1", 5)
    assert len(got.of(A, "ok")) == 1  # the blocklist only ever removes stock phrases


async def test_only_blocklisted_phrases_are_suppressed(make):
    scheduler, got = await make(Phrase("We ship on Friday."), blocklist=("thank you",))
    scheduler.submit(window(A, 1, speech_fraction=0.1))
    await scheduler.drain("m1", 5)
    assert len(got.of(A, "ok")) == 1


# --- drain -----------------------------------------------------------------------------------


async def test_drain_returns_once_the_meetings_windows_are_done(make):
    scheduler, got = await make(FakeAdapter(delay_s=0.02), queue_max=20)
    for n in range(1, 6):
        scheduler.submit(window(A, n))
    started = time.monotonic()
    result = await scheduler.drain("m1", 5)
    assert result.drained and result.remaining == 0 and time.monotonic() - started < 3
    assert len(got.of(A, "ok")) == 5


async def test_drain_times_out_and_reports_what_is_left(make):
    gate = threading.Event()
    scheduler, _ = await make(FakeAdapter(delay_s=lambda d, w: gate.wait(5) and 0), queue_max=10)
    for n in range(1, 5):
        scheduler.submit(window(A, n))
    result = await scheduler.drain("m1", 0.2)
    assert not result.drained and result.remaining == 4
    gate.set()
    assert (await scheduler.drain("m1", 5)).drained


async def test_drain_waits_only_for_its_own_meeting(make):
    gate = threading.Event()

    def delay(device, n):
        return gate.wait(5) and 0 if device == A else 0.0

    scheduler, _ = await make(FakeAdapter(delay_s=delay), workers=2, queue_max=10)
    scheduler.submit(window(A, 1, meeting="busy"))
    scheduler.submit(window(B, 1, meeting="other"))
    assert (await scheduler.drain("other", 3)).drained  # not held up by the other meeting's stuck window
    assert not (await scheduler.drain("busy", 0.1)).drained
    gate.set()
    assert (await scheduler.drain("busy", 5)).drained


async def test_drain_of_an_idle_or_unknown_meeting_is_immediate(make):
    scheduler, _ = await make()
    assert (await scheduler.drain("nobody", 1)).drained


# --- compute priority hook -------------------------------------------------------------------


async def test_a_reasoning_caller_waits_while_stt_is_backed_up_then_proceeds():
    backlog = {"n": 10}
    priority = ComputePriority(lambda: backlog["n"], high_backlog_windows=4, max_wait_s=3.0, poll_s=0.01)
    task = asyncio.create_task(priority.wait_for_turn("reasoning"))
    await asyncio.sleep(0.1)
    assert not task.done()  # STT is busy: the caller is held back
    backlog["n"] = 2
    turn = await asyncio.wait_for(task, 2)
    assert not turn.timed_out and turn.waited_s >= 0.1 and turn.backlog == 2
    assert priority.waits == 1 and priority.timeouts == 0


async def test_the_wait_has_a_documented_maximum_so_reasoning_is_never_starved():
    priority = ComputePriority(lambda: 50, high_backlog_windows=4, max_wait_s=0.2, poll_s=0.01)
    started = time.monotonic()
    turn = await priority.wait_for_turn("embedding")
    assert turn.timed_out and 0.19 <= time.monotonic() - started < 1.0 and turn.backlog == 50
    assert priority.timeouts == 1


async def test_no_wait_when_stt_is_idle_or_at_the_threshold():
    priority = ComputePriority(lambda: 4, high_backlog_windows=4, max_wait_s=1.0)
    turn = await priority.wait_for_turn()
    assert turn.waited_s < 0.05 and not turn.timed_out and priority.waits == 0


async def test_the_hook_reads_the_live_scheduler_backlog(make):
    gate = threading.Event()
    scheduler, _ = await make(FakeAdapter(delay_s=lambda d, w: gate.wait(5) and 0), queue_max=20)
    priority = ComputePriority(scheduler.total_backlog, high_backlog_windows=2, max_wait_s=5.0, poll_s=0.01)
    for n in range(1, 8):
        scheduler.submit(window(A, n))
    task = asyncio.create_task(priority.wait_for_turn("reasoning"))
    await asyncio.sleep(0.15)
    assert not task.done()
    gate.set()
    assert not (await asyncio.wait_for(task, 5)).timed_out
