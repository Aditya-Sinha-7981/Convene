import asyncio
from dataclasses import replace

import numpy as np
import pytest
from aiohttp import ClientSession

from server.config import PipelineConfig
from server.pipeline.adapter import FakeAdapter, SttResult
from server.repositories import utterances
from tests.support.server import settings_in, start_server
from tests.support.synthetic_phone import SAMPLE_RATE, SyntheticPhone
from tests.support.util import wait_for


class DeviceTextAdapter(FakeAdapter):
    def transcribe_window(self, device_id, window_id, audio):
        return SttResult(f"said-by-{device_id[:8]}", .9)


def burst(freq):
    t = np.arange(SAMPLE_RATE) / SAMPLE_RATE
    return (np.sin(2 * np.pi * freq * t) * ((t % .5) < .25) * .3 * 32767).astype("<i2")


@pytest.mark.asyncio
async def test_two_synthetic_phones_store_distinct_attributed_lines(tmp_path):
    settings = replace(settings_in(tmp_path), pipeline=replace(PipelineConfig(), segmentation="fixed"))
    server = await start_server(settings, stt_adapter=DeviceTextAdapter())
    phones = []
    try:
        async with ClientSession() as http, http.post(server.base_url + "/api/meetings", json={}) as response:
            meeting_id = (await response.json())["meeting"]["meeting_id"]
        phones = [SyntheticPhone(server.base_url, meeting_id, name="A", samples=burst(440)),
                  SyntheticPhone(server.base_url, meeting_id, name="B", samples=burst(880))]
        await asyncio.gather(*(phone.connect() for phone in phones))
        await asyncio.gather(*(phone.wait_connected() for phone in phones))
        await wait_for(lambda: len(utterances.list_for_meeting(server.runtime.db.conn, meeting_id)) >= 2,
                       timeout=20)
        rows = utterances.list_for_meeting(server.runtime.db.conn, meeting_id)
        by_device = {phone.device_id: [row for row in rows if row.device_id == phone.device_id] for phone in phones}
        assert all(by_device.values())
        assert all(row.text == f"said-by-{device_id[:8]}" and row.participant_id is not None
                   for device_id, own_rows in by_device.items() for row in own_rows)
    finally:
        await asyncio.gather(*(phone.close() for phone in phones), return_exceptions=True)
        await server.stop()
