import numpy as np
import pytest

from server.config import load_settings
from server.rag.embedding import build_embedding_adapter
from server.rag.embedding import FakeEmbeddingAdapter


def test_fake_embedding_is_deterministic_normalized_and_has_a_token_counter():
    adapter = FakeEmbeddingAdapter()
    first, second = adapter.embed(["same", "same"])
    assert np.array_equal(first, second)
    assert round(float(np.linalg.norm(first)), 6) == 1.0
    assert adapter.count_tokens("one two three") == 3


@pytest.mark.model
def test_real_embedding_model_runs_from_cache_and_ranks_related_text_first(monkeypatch):
    monkeypatch.setenv("HF_HUB_OFFLINE", "1")
    adapter = build_embedding_adapter(load_settings().embedding)
    adapter.load()
    chunks = ["[Asha, 00:00:01] The budget is forty thousand rupees.",
              "[Bharat, 00:00:02] The design review is on Tuesday."]
    vectors = adapter.embed(chunks)
    question = adapter.embed(["What is the project budget?"])[0]
    assert int(np.argmax([float(np.dot(question, vector)) for vector in vectors])) == 0
