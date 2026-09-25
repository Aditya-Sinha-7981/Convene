"""Q&A test support: a fake embedder with controllable relevance, and a seeded, indexed meeting."""
import numpy as np

from server import registry
from server.config import QaConfig, RagConfig
from server.ids import new_id
from server.rag.indexer import TranscriptIndexer
from server.repositories import utterances
from server.repositories.models import Utterance
from server.timeutil import utc_now

TOPICS = ("budget", "launch", "hotel", "intern", "crash", "churn", "latency", "design")


class TopicEmbedding:
    """Each topic word owns a dimension, plus a small constant so every vector is non-zero. A question shares a
    direction with a chunk exactly when they mention the same topics, so relevance is predictable."""
    runtime = "sentence_transformers"

    def __init__(self, dimension: int = 384, fail: Exception | None = None):
        self.dimension, self.fail, self.model_identifier = dimension, fail, "fake/topic-embedding"
        self.calls = 0

    def load(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return len(text.split())

    def embed(self, texts):
        self.calls += 1
        if self.fail is not None:
            raise self.fail
        vectors = []
        for text in texts:
            vector = np.zeros(self.dimension, dtype=np.float32)
            vector[-1] = 0.1
            for index, topic in enumerate(TOPICS):
                vector[index] = text.lower().count(topic)
            vectors.append(vector / np.linalg.norm(vector))
        return vectors


QA = QaConfig(top_k=3, min_similarity=0.6, answer_timeout_s=2.0)
RAG = RagConfig(target_tokens=5, hard_max_tokens=60, settle_delay_s=.01, retry_delay_s=.01)


def seed_meeting(db, lines, *, title="Review", created_at=None):
    """``lines`` is a list of (speaker display name, second, text); returns (meeting, [Utterance])."""
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, title)
        people = {}
        rows = []
        for name, second, text in lines:
            if name not in people:
                people[name] = registry.register_device(tx, meeting.meeting_id, new_id(), name)
            registration = people[name]
            rows.append(utterances.insert(tx.conn, Utterance(
                new_id(), meeting.meeting_id, registration.device.device_id,
                registration.participants[0].participant_id, text,
                f"2026-09-26T10:{second // 60:02d}:{second % 60:02d}.000Z",
                f"2026-09-26T10:{second // 60:02d}:{second % 60:02d}.900Z",
                .9, "device", .95, created_at or utc_now())))
    return meeting, rows


async def indexed(db, embedding, meeting_id):
    """A started indexer with the meeting's chunks embedded (and closed)."""
    indexer = TranscriptIndexer(db, embedding, RAG)
    await indexer.start()
    await indexer.flush(meeting_id)
    return indexer
