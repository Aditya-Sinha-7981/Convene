"""CON-08 transcript ingestion. Retrieval deliberately belongs to CON-09."""

from .chunker import ChunkInput, ChunkSpec, chunk_utterances

__all__ = ["ChunkInput", "ChunkSpec", "chunk_utterances"]
