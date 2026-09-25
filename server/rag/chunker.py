"""Pure deterministic transcript chunking.

The rendered line is ``[<speaker>, <elapsed HH:MM:SS>] <text>``.  Metadata is part of the
embedding input, therefore every capacity decision counts the complete rendered text.  The default
counter is a deliberately conservative word/punctuation approximation; the real embedding adapter
supplies its tokenizer before a chunk reaches a model.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Sequence


_TOKENS = re.compile(r"\w+|[^\w\s]", re.UNICODE)
_SENTENCES = re.compile(r"(?<=[.!?])\s+")


@dataclass(frozen=True)
class ChunkInput:
    utterance_id: str
    speaker_label: str
    t_start: str
    text: str
    split: bool = False  # one sentence-split piece of an over-cap utterance


@dataclass(frozen=True)
class ChunkSpec:
    utterance_id_start: str
    utterance_id_end: str
    chunk_index: int
    text: str
    split: bool = False  # the chunk is exactly one piece of an over-cap utterance


def approximate_tokens(text: str) -> int:
    return len(_TOKENS.findall(text))


def _elapsed(t_start: str, meeting_started_at: str) -> str:
    start = datetime.fromisoformat(meeting_started_at.replace("Z", "+00:00"))
    instant = datetime.fromisoformat(t_start.replace("Z", "+00:00"))
    seconds = max(0, int((instant - start).total_seconds()))
    return f"{seconds // 3600:02d}:{(seconds % 3600) // 60:02d}:{seconds % 60:02d}"


def render_line(item: ChunkInput, meeting_started_at: str, text: str | None = None) -> str:
    return f"[{item.speaker_label}, {_elapsed(item.t_start, meeting_started_at)}] {text if text is not None else item.text.strip()}"


def _split_overlong(item: ChunkInput, meeting_started_at: str, hard_max_tokens: int,
                    count_tokens: Callable[[str], int]) -> list[ChunkInput]:
    """Split just one overlong utterance at sentence boundaries; a pathological sentence is word-split."""
    sentences = [part.strip() for part in _SENTENCES.split(item.text.strip()) if part.strip()] or [item.text.strip()]
    pieces, current = [], ""
    for sentence in sentences:
        candidate = f"{current} {sentence}".strip()
        if current and count_tokens(render_line(item, meeting_started_at, candidate)) > hard_max_tokens:
            pieces.append(current)
            current = sentence
        else:
            current = candidate
        while count_tokens(render_line(item, meeting_started_at, current)) > hard_max_tokens:
            words = current.split()
            taken = ""
            while words and count_tokens(render_line(item, meeting_started_at, f"{taken} {words[0]}".strip())) <= hard_max_tokens:
                taken = f"{taken} {words.pop(0)}".strip()
            # A metadata line itself cannot exceed the configured cap in supported configurations.
            if not taken:
                raise ValueError("hard_max_tokens is too small for rendered transcript metadata")
            pieces.append(taken)
            current = " ".join(words)
    if current:
        pieces.append(current)
    return [ChunkInput(item.utterance_id, item.speaker_label, item.t_start, piece, split=len(pieces) > 1)
            for piece in pieces]


def chunk_utterances(utterances: Sequence[ChunkInput], meeting_started_at: str, *, target_tokens: int,
                     hard_max_tokens: int, count_tokens: Callable[[str], int] = approximate_tokens) -> list[ChunkSpec]:
    """Return stable chunks, preferring a speaker boundary after reaching target size.

    Chunks never combine text across an arbitrary mid-utterance boundary. A single over-cap utterance is
    represented by contiguous, same-ID chunks after its sentence split, one piece per chunk and never sharing a
    chunk with another utterance, so chunk utterance ranges never overlap and each range can be rebuilt alone.
    """
    if not 1 <= target_tokens <= hard_max_tokens:
        raise ValueError("target_tokens must be positive and no greater than hard_max_tokens")
    expanded: list[ChunkInput] = []
    for item in utterances:
        if not item.text.strip():
            continue
        expanded.extend(_split_overlong(item, meeting_started_at, hard_max_tokens, count_tokens))
    chunks: list[list[ChunkInput]] = []
    current: list[ChunkInput] = []
    for item in expanded:
        if item.split:
            if current:
                chunks.append(current)
            chunks.append([item])
            current = []
            continue
        candidate = "\n".join(render_line(part, meeting_started_at) for part in [*current, item])
        # Once target is reached, honor the natural speaker turn. Hard capacity always wins.
        speaker_changed = bool(current and current[-1].speaker_label != item.speaker_label)
        current_text = "\n".join(render_line(part, meeting_started_at) for part in current)
        if current and ((count_tokens(current_text) >= target_tokens and speaker_changed)
                        or count_tokens(candidate) > hard_max_tokens):
            chunks.append(current)
            current = [item]
        else:
            current.append(item)
    if current:
        chunks.append(current)
    return [ChunkSpec(chunk[0].utterance_id, chunk[-1].utterance_id, index,
                      "\n".join(render_line(item, meeting_started_at) for item in chunk), chunk[0].split)
            for index, chunk in enumerate(chunks)]
