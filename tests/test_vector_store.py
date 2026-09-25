import numpy as np
import pytest

from server.errors import ValidationError
from server.ids import new_id
from server import registry
from server.rag.vector_store import VectorStore
from server.repositories.models import TranscriptChunk, Utterance
from server.repositories import transcript_chunks, utterances
from server.timeutil import utc_now


def chunk(meeting_id, utterance_id, index):
    now = utc_now()
    return TranscriptChunk(new_id(), meeting_id, utterance_id, utterance_id, "text", index, "pending", now)


def test_vector_store_guards_model_and_scopes_knn_by_meeting(db):
    store = VectorStore(384, "test-model")
    x = np.eye(384, dtype=np.float32)[0]
    y = np.eye(384, dtype=np.float32)[1]
    with db.transaction() as tx:
        a, b = registry.create_meeting(tx, "A"), registry.create_meeting(tx, "B")
        da = registry.register_device(tx, a.meeting_id, new_id(), "Asha")
        dbb = registry.register_device(tx, b.meeting_id, new_id(), "Bharat")
        def add(meeting, registration, second):
            utterance = Utterance(new_id(), meeting.meeting_id, registration.device.device_id,
                                  registration.participants[0].participant_id, "text",
                                  f"2026-09-26T10:00:0{second}.000Z", f"2026-09-26T10:00:0{second}.000Z",
                                  .9, "device", .95, utc_now())
            return utterances.insert(tx.conn, utterance)
        ua1, ua2, ub = add(a, da, 1), add(a, da, 2), add(b, dbb, 1)
        one, two, other = chunk(a.meeting_id, ua1.utterance_id, 0), chunk(a.meeting_id, ua2.utterance_id, 1), chunk(b.meeting_id, ub.utterance_id, 0)
        for row in (one, two, other):
            transcript_chunks.insert(tx.conn, row)
        store.guard_model(tx.conn)
        store.upsert(tx.conn, one.chunk_id, a.meeting_id, x)
        store.upsert(tx.conn, two.chunk_id, a.meeting_id, y)
        store.upsert(tx.conn, other.chunk_id, b.meeting_id, x)
    assert [hit.chunk_id for hit in store.search(db.conn, a.meeting_id, x, 5)] == [one.chunk_id, two.chunk_id]
    assert transcript_chunks.get(db.conn, one.chunk_id).status == "ready"
    with pytest.raises(ValidationError, match="differs"):
        VectorStore(384, "other-model").guard_model(db.conn)
