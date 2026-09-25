from server.rag.chunker import ChunkInput, approximate_tokens, chunk_utterances


START = "2026-09-26T10:00:00.000Z"


def item(utterance_id, speaker, second, text):
    return ChunkInput(utterance_id, speaker, f"2026-09-26T10:00:{second:02d}.000Z", text)


def test_chunker_renders_corrected_labels_metadata_and_is_deterministic():
    rows = [item("a", "Asha", 3, "We will ship Friday."), item("b", "Speaker on Phone 2", 8, "I can test it.")]
    first = chunk_utterances(rows, START, target_tokens=8, hard_max_tokens=30)
    assert first == chunk_utterances(rows, START, target_tokens=8, hard_max_tokens=30)
    assert first[0].text == "[Asha, 00:00:03] We will ship Friday."
    assert "[Speaker on Phone 2, 00:00:08] I can test it." in first[1].text


def test_chunker_prefers_speaker_turn_and_counts_rendered_metadata():
    rows = [item("a", "Asha", 1, "one two three"), item("b", "Bharat", 2, "four five six")]
    chunks = chunk_utterances(rows, START, target_tokens=7, hard_max_tokens=20)
    assert [(chunk.utterance_id_start, chunk.utterance_id_end) for chunk in chunks] == [("a", "a"), ("b", "b")]
    assert all(approximate_tokens(chunk.text) <= 20 for chunk in chunks)


def test_overlong_single_utterance_splits_at_sentence_boundaries_without_losing_text():
    rows = [item("a", "Asha", 1, "One two three. Four five six. Seven eight nine.")]
    chunks = chunk_utterances(rows, START, target_tokens=8, hard_max_tokens=12)
    assert len(chunks) >= 2
    assert all(chunk.utterance_id_start == chunk.utterance_id_end == "a" for chunk in chunks)
    assert " ".join(chunk.text.split("] ", 1)[1] for chunk in chunks) == rows[0].text
