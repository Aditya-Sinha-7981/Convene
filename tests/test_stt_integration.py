"""The STT pipeline inside the real server: synthetic phones -> WebRTC -> sink seam -> resample, VAD, windowing ->
scheduler -> a fake adapter -> the window callback, ModelExecution rows, audit events and gauges.

Loopback and a fake model: this shows the plumbing, isolation and accounting work end to end. It does not show
transcription quality (the ``model`` tests in test_stt_adapter.py) or anything about real phones.
"""
import asyncio
import json
import time
from dataclasses import replace

import numpy as np
import pytest
from aiohttp import ClientSession, WSMsgType

from server.config import PipelineConfig
from server.pipeline.adapter import FakeAdapter, SttResult
from server.repositories import audit_events, model_executions
from tests.support.server import ServerStartupError, settings_in, start_server
from tests.support.speech import RATE, db
from tests.support.synthetic_phone import SAMPLE_RATE, SyntheticPhone
from tests.support.util import wait_for


def burst_tone(freq: float, level_db: float = -20.0) -> np.ndarray:
    """Speech-like for a VAD (0.25 s on, 0.25 s off), and identifiable by its frequency. 48 kHz, one second, looped."""
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    on = ((t % 0.5) < 0.25).astype(np.float32)
    return (np.sin(2 * np.pi * freq * t) * db(level_db) * 1.4 * on * 32767).astype("<i2")


class FrequencyAdapter(FakeAdapter):
    """Reports the dominant frequency of the window it was given, so a test can tell whose audio it heard."""

    def transcribe_window(self, device_id, window_id, audio):
        with self._lock:
            self.calls.append((device_id, window_id, len(audio)))
        delay = self.delay_s(device_id, window_id) if callable(self.delay_s) else self.delay_s
        if delay:
            time.sleep(delay)
        spectrum = np.abs(np.fft.rfft(audio * np.hanning(len(audio))))
        hz = int(round(np.argmax(spectrum) * RATE / len(audio) / 10) * 10)
        return SttResult(f"f{hz}", 0.9)


async def new_meeting(server) -> str:
    async with ClientSession() as http, http.post(server.base_url + "/api/meetings", json={}) as r:
        return (await r.json())["meeting"]["meeting_id"]


def stt_settings(tmp_path, **pipeline):
    """Settings for a server under test. The plumbing tests use fixed 1 s windows (their bursts are timed for it);
    segment mode has its own tests below, and pass ``segmentation="segments"``."""
    base = settings_in(tmp_path)
    return replace(base, pipeline=replace(PipelineConfig(), **{"segmentation": "fixed", **pipeline}))


@pytest.fixture
async def stt(tmp_path):
    """A server with a frequency-reporting adapter and a collector for every window outcome."""
    adapter = FrequencyAdapter()
    server = await start_server(stt_settings(tmp_path), stt_adapter=adapter)
    outcomes = []
    server.runtime.on_transcribed_window(outcomes.append)
    phones = []

    async def phone(meeting_id, freq, name="P", **kw) -> SyntheticPhone:
        p = SyntheticPhone(server.base_url, meeting_id, name=name, samples=burst_tone(freq), **kw)
        phones.append(p)
        await p.connect()
        await p.wait_connected()
        return p

    yield server, adapter, outcomes, phone
    for p in phones:
        await p.close()
    await server.stop()


def by_device(outcomes, device_id, status="ok"):
    return [o for o in outcomes if o.device_id == device_id and o.status == status]


# --- startup and accounting -----------------------------------------------------------------


async def test_startup_loads_the_model_records_model_load_and_makes_the_pipeline_the_sink(stt):
    server, adapter, _, _ = stt
    assert adapter.loaded is True and server.runtime.pipeline is not None and server.runtime.sink is server.runtime.pipeline
    (event,) = audit_events.list_events(server.runtime.db.conn, event_type="model_load")
    assert event.component == "models" and event.meeting_id is None
    assert event.payload["resource_type"] == "stt" and event.payload["model_identifier"] == "fake/stt"
    assert event.payload["runtime"] == "mlx" and event.payload["duration_ms"] >= 0


async def test_a_model_that_fails_to_load_stops_startup(tmp_path):
    class Broken(FakeAdapter):
        def load(self):
            raise RuntimeError("weights are corrupt")

    with pytest.raises(ServerStartupError, match="exited during startup"):
        await start_server(settings_in(tmp_path), stt_adapter=Broken())
    from server.repositories import audit_events as events
    from server.db import Database
    db = Database.open(tmp_path / "convene.db")
    try:
        assert events.list_events(db.conn, event_type="model_load") == []  # a model that never loaded is never reported loaded
    finally:
        db.close()


async def test_each_phones_audio_is_transcribed_under_its_own_device_with_no_cross_talk(stt):
    server, adapter, outcomes, phone = stt
    meeting_id = await new_meeting(server)
    a = await phone(meeting_id, 440, "A")
    b = await phone(meeting_id, 880, "B")
    await wait_for(lambda: len(by_device(outcomes, a.device_id)) >= 3 and len(by_device(outcomes, b.device_id)) >= 3, timeout=20)
    heard_a = {o.text for o in by_device(outcomes, a.device_id)}
    heard_b = {o.text for o in by_device(outcomes, b.device_id)}
    assert heard_a <= {"f440", "f430", "f450"} and heard_b <= {"f880", "f870", "f890"}, (heard_a, heard_b)
    assert heard_a.isdisjoint(heard_b)  # the audio survived Opus, resampling and windowing intact, per device
    assert all(o.meeting_id == meeting_id and o.stt_confidence == 0.9 for o in outcomes if o.status == "ok")


async def test_windows_carry_monotonic_ids_contiguous_utc_times_and_the_model_rate(stt):
    server, _, outcomes, phone = stt
    meeting_id = await new_meeting(server)
    a = await phone(meeting_id, 440)
    began = time.time()
    await wait_for(lambda: len(by_device(outcomes, a.device_id)) >= 4, timeout=20)
    mine = sorted(by_device(outcomes, a.device_id), key=lambda o: o.window_id)
    assert [o.window_id for o in mine] == sorted({o.window_id for o in mine})
    from tests.test_windowing import parse
    for o in mine:
        assert o.sample_rate == 16000 and o.n_samples == 16000
        assert abs(parse(o.t_end) - parse(o.t_start) - 1.0) < 0.01
        assert began - 30 < parse(o.t_start) < time.time() + 1  # UTC wall clock, not a stream-relative time
    for x, y in zip(mine, mine[1:]):
        if y.window_id == x.window_id + 1:
            assert x.t_end == y.t_start


async def test_every_invocation_is_recorded_and_no_audio_is_persisted(stt):
    server, adapter, outcomes, phone = stt
    meeting_id = await new_meeting(server)
    a = await phone(meeting_id, 440)
    await wait_for(lambda: len(by_device(outcomes, a.device_id)) >= 3, timeout=20)
    await server.runtime.pipeline.drain(meeting_id, 10)
    conn = server.runtime.db.conn
    rows = model_executions.list_executions(conn, resource_type="stt")
    assert len(rows) == len(adapter.calls) >= 3 and {r.model_identifier for r in rows} == {"fake/stt"}
    assert all(r.related_id.startswith(a.device_id + "/") for r in rows)
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert not any("audio" in t.lower() or "window" in t.lower() for t in tables)  # nothing stores samples


# --- isolation and overload -----------------------------------------------------------------


async def test_a_failing_window_on_one_phone_is_audited_and_the_other_phone_and_later_windows_continue(tmp_path):
    adapter = FrequencyAdapter(fail_when=None)
    fail = {"device": None}
    real = adapter.transcribe_window

    def flaky(device_id, window_id, audio):
        if device_id == fail["device"] and window_id in (2, 3):
            raise RuntimeError("model crashed on this window")
        return real(device_id, window_id, audio)

    adapter.transcribe_window = flaky
    server = await start_server(stt_settings(tmp_path), stt_adapter=adapter)
    outcomes = []
    server.runtime.on_transcribed_window(outcomes.append)
    phones = []
    try:
        meeting_id = await new_meeting(server)
        a, b = (SyntheticPhone(server.base_url, meeting_id, samples=burst_tone(f)) for f in (440, 880))
        phones += [a, b]
        fail["device"] = a.device_id
        for p in (a, b):
            await p.connect()
        for p in (a, b):
            await p.wait_connected()
        await wait_for(lambda: len(by_device(outcomes, a.device_id)) >= 3 and len(by_device(outcomes, b.device_id)) >= 4, timeout=25)
        failed = by_device(outcomes, a.device_id, "failed")
        assert sorted(o.window_id for o in failed) == [2, 3] and all("crashed" in o.error for o in failed)
        assert not by_device(outcomes, b.device_id, "failed")  # B is untouched
        assert max(o.window_id for o in by_device(outcomes, a.device_id)) > 3  # and A's later windows still succeed
        errors = audit_events.list_events(server.runtime.db.conn, event_type="model_error")
        assert sorted(e.payload["window_id"] for e in errors) == [2, 3]
        assert {e.payload["device_id"] for e in errors} == {a.device_id} and all(e.meeting_id == meeting_id for e in errors)
    finally:
        for p in phones:
            await p.close()
        await server.stop()


async def test_a_slow_model_backs_up_that_phones_queue_visibly_and_drops_are_counted_and_audited(tmp_path):
    slow = FrequencyAdapter(delay_s=lambda d, w: 1.2)  # slower than the audio arrives (about one window a second)
    server = await start_server(stt_settings(tmp_path, queue_max=2), stt_adapter=slow)
    outcomes = []
    server.runtime.on_transcribed_window(outcomes.append)
    phone = None
    try:
        meeting_id = await new_meeting(server)
        async with ClientSession() as http:
            ws = await http.ws_connect(server.ws_url(f"/ws/dashboard/{meeting_id}"))
            phone = SyntheticPhone(server.base_url, meeting_id, samples=burst_tone(440))
            await phone.connect()
            await phone.wait_connected()
            backlog, dropped = [], []
            deadline = time.monotonic() + 20
            while time.monotonic() < deadline and not (dropped and max(backlog, default=0) >= 1):
                try:
                    message = await asyncio.wait_for(ws.receive(), 1.0)
                except asyncio.TimeoutError:
                    continue
                if message.type == WSMsgType.TEXT:
                    data = json.loads(message.data)
                    if data["type"] == "device_gauges":
                        backlog.append(data["gauges"][0]["stt_backlog"])
                        dropped.append(data["gauges"][0]["stt_dropped_windows"]) if data["gauges"][0]["stt_dropped_windows"] else None
            await ws.close()
        assert max(backlog) >= 1, "the backlog must be visible on the dashboard feed"
        assert dropped, "drops must be visible on the dashboard feed"
        stats = server.runtime.pipeline.scheduler.stats()[phone.device_id]
        assert stats["dropped"] >= 1 and stats["depth"] <= 3  # bounded: queue_max 2 plus one in flight
        events = audit_events.list_events(server.runtime.db.conn, event_type="stt_window_dropped")
        assert len(events) >= stats["dropped"] - 1 and all(e.payload["reason"] == "overload" for e in events)
        assert by_device(outcomes, phone.device_id, "dropped")
    finally:
        if phone:
            await phone.close()
        await server.stop()


async def test_gauges_in_the_meeting_snapshot_carry_the_pipelines_backlog_and_drop_counters(stt):
    server, _, outcomes, phone = stt
    meeting_id = await new_meeting(server)
    a = await phone(meeting_id, 440)
    await wait_for(lambda: by_device(outcomes, a.device_id), timeout=20)
    async with ClientSession() as http, http.get(f"{server.base_url}/api/meetings/{meeting_id}") as r:
        (device,) = (await r.json())["devices"]
    assert isinstance(device["gauges"]["stt_backlog"], int) and device["gauges"]["stt_backlog"] >= 0
    assert device["gauges"]["stt_dropped_windows"] == 0
    async with ClientSession() as http, http.get(server.base_url + "/metrics") as r:
        assert "stt_backlog" in (await r.json())[0]


async def test_each_transcribed_window_is_printed_on_the_console_with_the_speakers_name(stt, caplog):
    import logging
    server, _, outcomes, phone = stt
    meeting_id = await new_meeting(server)
    with caplog.at_level(logging.INFO, logger="convene.transcript"):
        a = await phone(meeting_id, 440, "Priya")
        await wait_for(lambda: by_device(outcomes, a.device_id), timeout=20)
        await wait_for(lambda: any("STT [Priya]" in r.getMessage() for r in caplog.records), timeout=5)
    line = next(r.getMessage() for r in caplog.records if r.name == "convene.transcript")
    assert line.startswith("STT [Priya] #1 ") and "conf=0.90" in line and line.rstrip().endswith("f440")


async def test_console_transcripts_can_be_turned_off_and_nothing_else_changes(tmp_path, caplog):
    import logging
    server = await start_server(stt_settings(tmp_path, log_transcripts=False), stt_adapter=FrequencyAdapter())
    outcomes = []
    server.runtime.on_transcribed_window(outcomes.append)
    phone = None
    try:
        meeting_id = await new_meeting(server)
        with caplog.at_level(logging.INFO, logger="convene.transcript"):
            phone = SyntheticPhone(server.base_url, meeting_id, name="Priya", samples=burst_tone(440))
            await phone.connect()
            await phone.wait_connected()
            await wait_for(lambda: by_device(outcomes, phone.device_id), timeout=20)
        assert not [r for r in caplog.records if r.name == "convene.transcript"]
    finally:
        if phone:
            await phone.close()
        await server.stop()


async def test_metrics_carry_a_per_phone_stt_block_with_the_vad_and_queue_counters(stt):
    server, _, outcomes, phone = stt
    meeting_id = await new_meeting(server)
    a = await phone(meeting_id, 440)
    await wait_for(lambda: by_device(outcomes, a.device_id), timeout=20)
    async with ClientSession() as http, http.get(server.base_url + "/metrics") as r:
        (row,) = [x for x in await r.json() if x["device_id"] == a.device_id]
    block = row["stt"]
    assert block["windows_seen"] >= block["windows_passed"] >= block["transcribed"] >= 1
    assert block["windows_gated"] + block["windows_passed"] <= block["windows_seen"]
    assert isinstance(block["vad_noise_floor_db"], float) and block["failed"] == 0 and block["dropped"] == 0


# --- time base across devices ---------------------------------------------------------------


async def test_windows_from_two_phones_order_plausibly_by_t_start(stt):
    server, _, outcomes, phone = stt
    meeting_id = await new_meeting(server)
    a = await phone(meeting_id, 440, "early")
    await asyncio.sleep(2.5)  # B joins well after A started speaking
    b = await phone(meeting_id, 880, "late")
    await wait_for(lambda: by_device(outcomes, a.device_id) and len(by_device(outcomes, b.device_id)) >= 2, timeout=25)
    from tests.test_windowing import parse
    first_a = min(parse(o.t_start) for o in by_device(outcomes, a.device_id))
    first_b = min(parse(o.t_start) for o in by_device(outcomes, b.device_id))
    assert 1.5 < first_b - first_a < 8.0, f"B's first window is {first_b - first_a:.2f} s after A's"


# --- ending a meeting: drain ------------------------------------------------------------------


async def test_a_meeting_end_hook_can_drain_so_queued_windows_finish_before_summarizing(stt):
    server, adapter, outcomes, phone = stt
    meeting_id = await new_meeting(server)
    a = await phone(meeting_id, 440)
    await wait_for(lambda: len(by_device(outcomes, a.device_id)) >= 2, timeout=20)
    drained = []

    async def hook(mid):
        drained.append(await server.runtime.pipeline.drain(mid, 15))

    server.runtime.on_meeting_ended(hook, "drain-first")
    async with ClientSession() as http, http.post(f"{server.base_url}/api/meetings/{meeting_id}/end") as r:
        assert r.status == 202
    (result,) = drained
    assert result.drained and result.remaining == 0
    assert server.runtime.pipeline.scheduler.pending_for_meeting(meeting_id) == 0
    assert audit_events.list_events(server.runtime.db.conn, event_type="hook_failed") == []


async def test_a_failing_transcribed_window_callback_never_disturbs_the_pipeline(stt):
    server, _, outcomes, phone = stt
    server.runtime.on_transcribed_window(lambda o: (_ for _ in ()).throw(RuntimeError("attribution bug")))
    meeting_id = await new_meeting(server)
    a = await phone(meeting_id, 440)
    await wait_for(lambda: len(by_device(outcomes, a.device_id)) >= 3, timeout=20)  # the healthy callback still gets everything
    assert server.runtime.pipeline.scheduler.stats()[a.device_id]["failed"] == 0


async def test_the_transport_only_mode_still_works_without_a_model(server, make_phone):
    """--no-stt: no adapter, audio goes to the counting sink, nothing is transcribed."""
    meeting_id = await new_meeting(server)
    phone = make_phone(meeting_id)
    await phone.connect()
    await phone.wait_connected()
    await wait_for(lambda: server.runtime.sink.samples.get(phone.device_id, 0) > 48000)
    assert server.runtime.pipeline is None and server.runtime.stt_adapter is None
    assert audit_events.list_events(server.runtime.db.conn, event_type="model_load") == []


# --- real model, real transport (pytest -m model) --------------------------------------------


@pytest.mark.model
async def test_real_speech_through_synthetic_phones_and_the_real_model_stays_with_each_phone(tmp_path):
    """Two phones play different speech fixtures over WebRTC (Opus, 48 kHz) at the same time; the real model's
    transcripts come back under the right device. Synthetic text-to-speech over loopback: it proves the whole
    path holds together, not accuracy on real phone audio."""
    from server.config import load_settings
    from server.pipeline.adapter import ModelNotProvisionedError
    from server.pipeline.mlx_whisper_adapter import build_adapter
    from tests.support.speech import FIXTURES

    adapter = build_adapter(load_settings().stt)
    try:
        adapter.load()
    except ModelNotProvisionedError as exc:
        pytest.skip(f"model not provisioned: {exc}")
    server = await start_server(stt_settings(tmp_path, segmentation="segments"), stt_adapter=adapter, stt_loaded=True)
    outcomes = []
    server.runtime.on_transcribed_window(outcomes.append)
    phones = []
    try:
        meeting_id = await new_meeting(server)
        a = SyntheticPhone(server.base_url, meeting_id, name="A", wav_path=FIXTURES / "ship_beta.wav")
        b = SyntheticPhone(server.base_url, meeting_id, name="B", wav_path=FIXTURES / "release_notes.wav")
        phones += [a, b]
        for p in (a, b):
            await p.connect()
        for p in (a, b):
            await p.wait_connected()
        await wait_for(lambda: len(by_device(outcomes, a.device_id)) >= 3 and len(by_device(outcomes, b.device_id)) >= 3,
                       timeout=60, message="each phone needs a few transcribed windows")
        text = {p.device_id: " ".join(o.text for o in by_device(outcomes, p.device_id)).lower() for p in (a, b)}
        assert "beta" in text[a.device_id] and "release" not in text[a.device_id]
        assert "release" in text[b.device_id] and "beta" not in text[b.device_id]
        assert all(0.0 < o.stt_confidence <= 1.0 for o in outcomes if o.status == "ok")
    finally:
        for p in phones:
            await p.close()
        await server.stop()
