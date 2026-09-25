"""Persistence for CON-09 questions. A ``QAQuery`` row is written once, with its final outcome."""
import json

from . import base

COLUMNS = ("query_id", "meeting_id", "mode", "question", "answer", "cited_chunk_ids", "status", "created_at")


def insert(conn, *, query_id: str, meeting_id: str | None, mode: str, question: str, answer: str | None,
           cited_chunk_ids: list[str], status: str, created_at: str) -> dict:
    row = {"query_id": query_id, "meeting_id": meeting_id, "mode": mode, "question": question, "answer": answer,
           "cited_chunk_ids": json.dumps(list(cited_chunk_ids)), "status": status, "created_at": created_at}
    base.insert(conn, "QAQuery", row)
    return row


def get(conn, query_id: str) -> dict | None:
    row = base.query_one(conn, "SELECT * FROM QAQuery WHERE query_id = ?", (query_id,))
    return {column: row[column] for column in COLUMNS} if row else None


def list_for_meeting(conn, meeting_id: str) -> list[dict]:
    return [{column: row[column] for column in COLUMNS} for row in base.query_all(
        conn, "SELECT * FROM QAQuery WHERE meeting_id = ? ORDER BY created_at, rowid", (meeting_id,))]
