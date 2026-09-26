"""CON-12 synthetic full path with real cached local models.

This is deliberately marked ``model``: two aiortc clients loop generated speech over loopback, then exercise
the real STT, embeddings, reasoning model, summary and deterministic DOCX export. It is useful regression
evidence, but cannot establish browser microphone, certificate, Wi-Fi, or room-acoustics behavior.
"""
from __future__ import annotations

import asyncio

import pytest
from aiohttp import ClientSession
from docx import Document

from server.config import load_settings
from server.pipeline.adapter import ModelNotProvisionedError
from server.pipeline.mlx_whisper_adapter import build_adapter
from server.rag.embedding import build_embedding_adapter
from server.rag.reasoning import build_reasoning_adapter
from tests.support.server import settings_in, start_server
from tests.support.speech import FIXTURES
from tests.support.synthetic_phone import SyntheticPhone
from tests.support.util import wait_for


async def create_meeting(base_url: str) -> str:
    async with ClientSession() as http, http.post(base_url + "/api/meetings", json={}) as response:
        assert response.status == 201
        return (await response.json())["meeting"]["meeting_id"]


async def request_json(session, method: str, url: str, **kwargs):
    async with session.request(method, url, **kwargs) as response:
        return response.status, await response.json()


@pytest.mark.model
@pytest.mark.asyncio
async def test_cached_local_models_complete_the_synthetic_demo_path(tmp_path):
    """Two synthetic phones -> labelled transcript -> QA -> summary -> readable DOCX.

    The expected questions intentionally mirror the demo card. The assertions are model-facing rather than
    brittle string equality: stored citations, not generated inline text, are the source of attribution proof.
    """
    configured = load_settings()
    stt = build_adapter(configured.stt)
    embedding = build_embedding_adapter(configured.embedding)
    reasoning = build_reasoning_adapter(configured.reasoning)
    try:
        stt.load()
        embedding.load()
        reasoning.load()
    except (ModelNotProvisionedError, RuntimeError) as exc:
        pytest.skip(f"local demo model not provisioned: {exc}")

    settings = settings_in(tmp_path)
    server = await start_server(settings, stt_adapter=stt, stt_loaded=True,
                                embedding_adapter=embedding, reasoning_adapter=reasoning,
                                reasoning_loaded=True)
    phones: list[SyntheticPhone] = []
    try:
        meeting_id = await create_meeting(server.base_url)
        priya = SyntheticPhone(server.base_url, meeting_id, name="Priya", wav_path=FIXTURES / "ship_beta.wav")
        marcus = SyntheticPhone(server.base_url, meeting_id, name="Marcus", wav_path=FIXTURES / "release_notes.wav")
        phones.extend((priya, marcus))
        for phone in phones:
            answer = await phone.connect()
            assert answer["type"] == "answer"
            await phone.wait_connected()

        await wait_for(
            lambda: len(server.runtime.db.conn.execute(
                "SELECT utterance_id FROM Utterance WHERE meeting_id = ?", (meeting_id,)).fetchall()) >= 2,
            timeout=90, message="synthetic speech must become attributed utterances",
        )
        await server.runtime.pipeline.drain(meeting_id, 30)
        await server.runtime.indexer.flush(meeting_id)
        transcript = await server.runtime.db.run(
            lambda tx: tx.conn.execute("SELECT text, participant_id FROM Utterance WHERE meeting_id = ?", (meeting_id,)).fetchall()
        )
        assert len(transcript) >= 2

        async with ClientSession() as http:
            qa_url = server.base_url + f"/api/meetings/{meeting_id}/qa"
            status, answered = await request_json(
                http, "POST", qa_url, json={"question": "What did we decide about the beta launch date?", "mode": "live"}
            )
            assert status == 200 and answered["query"]["status"] == "answered", answered
            assert answered["citations"]
            assert "Priya" in answered["citations"][0]["speakers"]

            status, absent = await request_json(
                http, "POST", qa_url, json={"question": "Who is responsible for the marketing budget?", "mode": "live"}
            )
            assert status == 200 and absent["query"]["status"] == "no_grounding", absent

            status, _ = await request_json(http, "POST", server.base_url + f"/api/meetings/{meeting_id}/end", json={})
            assert status in (200, 202)
            summary_url = server.base_url + f"/api/meetings/{meeting_id}/summary"

            summary = None
            deadline = asyncio.get_running_loop().time() + 150
            while summary is None:
                _, body = await request_json(http, "GET", summary_url)
                summary = body if body.get("summary") else None
                if summary is None:
                    if asyncio.get_running_loop().time() > deadline:
                        raise TimeoutError("real model summary must complete")
                    await asyncio.sleep(.1)
            assert summary["summary"]["summary_text"] and summary["action_items"]

            export_url = server.base_url + f"/api/meetings/{meeting_id}/export?format=docx"
            async with http.get(export_url) as response:
                assert response.status == 200
                docx_path = tmp_path / "minutes.docx"
                docx_path.write_bytes(await response.read())
        document = Document(docx_path)
        text = "\n".join(paragraph.text for paragraph in document.paragraphs)
        assert "Action items" in text and "Priya" in text
    finally:
        for phone in phones:
            await phone.close()
        await server.stop()
