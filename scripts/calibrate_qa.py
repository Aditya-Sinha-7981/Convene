"""Calibrate the Q&A relevance threshold and run the RAG honesty test (CON-09).

    .venv/bin/python scripts/calibrate_qa.py [--fixture tests/fixtures/qa/product_sync.json] [--out report.json]
    .venv/bin/python scripts/calibrate_qa.py --honesty [--threshold 0.55] [--repeats 3] [--out report.json]

The fixture meeting is written to a temporary database and indexed with the configured embedding model and
chunking settings, exactly as a live meeting would be. Then:

* threshold: every answerable and unanswerable question is embedded and searched with no threshold. The best
  cosine similarity per question is recorded for each class, and a threshold is recommended: the midpoint of
  the gap when the classes separate, otherwise the value that keeps every answerable question while dropping as
  many unanswerable ones as possible (the answer model's NO_GROUNDING instruction is then the second guard).
* honesty (``--honesty``, needs the reasoning model): every question is asked ``--repeats`` times through the
  real Q&A service. Answerable: correct when answered, the answer names an expected fact, and a cited chunk holds
  the evidence line. Unanswerable: any ``answered`` result is a fabrication. The proposed gate is zero.

Models load from the local cache only (the adapters enforce it). Nothing is written outside the temporary
directory except ``--out``.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import statistics
import sys
import tempfile
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from server import registry  # noqa: E402
from server.db import Database  # noqa: E402
from server.ids import new_id  # noqa: E402
from server.rag.indexer import TranscriptIndexer  # noqa: E402
from server.rag.qa import QAService  # noqa: E402
from server.repositories import transcript_chunks, utterances  # noqa: E402
from server.repositories.models import Utterance  # noqa: E402
from server.timeutil import utc_now  # noqa: E402

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "qa" / "product_sync.json"


def load_fixture(path: Path = DEFAULT_FIXTURE) -> dict:
    return json.loads(Path(path).read_text())


def _stamp(second: int, fraction: str) -> str:
    return f"2026-09-26T10:{second // 60:02d}:{second % 60:02d}.{fraction}Z"


def seed(db, fixture: dict) -> str:
    """Write the fixture meeting (one phone per speaker); return its meeting_id.

    Lines are written now, so every later question satisfies the as-of rule; ``t_start`` is fixture time."""
    written = utc_now()
    with db.transaction() as tx:
        meeting = registry.create_meeting(tx, "Calibration fixture")
        phones = {}
        for speaker, second, text in fixture["lines"]:
            if speaker not in phones:
                phones[speaker] = registry.register_device(tx, meeting.meeting_id, new_id(), speaker)
            phone = phones[speaker]
            utterances.insert(tx.conn, Utterance(
                new_id(), meeting.meeting_id, phone.device.device_id, phone.participants[0].participant_id, text,
                _stamp(second, "000"), _stamp(second, "900"), .9, "device", .95, written))
    return meeting.meeting_id


def recommend(answerable: list[float], unanswerable: list[float]) -> dict:
    low, high = min(answerable), max(unanswerable)
    if low > high:
        return {"threshold": round((low + high) / 2, 3), "separable": True, "unanswerable_passing": 0}
    threshold = round(low - 0.005, 3)  # keep every answerable question
    return {"threshold": threshold, "separable": False,
            "unanswerable_passing": sum(score >= threshold for score in unanswerable)}


def _summary(scores: list[float]) -> dict:
    return {"n": len(scores), "min": round(min(scores), 4), "median": round(statistics.median(scores), 4),
            "max": round(max(scores), 4)}


async def threshold_report(service: QAService, meeting_id: str, fixture: dict) -> dict:
    """Best similarity per question with no threshold. Uses the service's own embedding and search."""
    from server.rag.retrieval import search_meeting  # the calibration harness is the second, offline caller
    loop = asyncio.get_running_loop()
    rows = {"answerable": [], "unanswerable": []}
    for kind in rows:
        for item in fixture[kind]:
            vector = (await loop.run_in_executor(None, service.embedding.embed, [item["question"]]))[0]
            found = await service.db.run(lambda tx: search_meeting(
                tx.conn, service.store, meeting_id, vector, top_k=service.config.top_k, min_similarity=-1.0,
                asked_at="9999-12-31T23:59:59.999Z"))
            top = found.evidence[0].chunk.text if found.evidence else ""
            evidence = item.get("evidence", [])
            evidence = evidence if isinstance(evidence, list) else [evidence]
            rows[kind].append({"question": item["question"], "best_similarity": round(found.best_similarity, 4),
                               "top_chunk_has_evidence": any(line in top for line in evidence) if kind == "answerable"
                               else None})
    answerable = [row["best_similarity"] for row in rows["answerable"]]
    unanswerable = [row["best_similarity"] for row in rows["unanswerable"]]
    return {"answerable": _summary(answerable), "unanswerable": _summary(unanswerable),
            "top_chunk_has_evidence": sum(bool(row["top_chunk_has_evidence"]) for row in rows["answerable"]),
            "recommendation": recommend(answerable, unanswerable), "questions": rows}


async def honesty_report(service: QAService, meeting_id: str, fixture: dict, repeats: int) -> dict:
    results = []
    for kind in ("answerable", "unanswerable"):
        for item in fixture[kind]:
            for attempt in range(repeats):
                began = time.monotonic()
                response = await service.ask(meeting_id, {"question": item["question"]})
                query = response["query"]
                row = {"kind": kind, "question": item["question"], "attempt": attempt, "status": query["status"],
                       "reason": response["reason"], "answer": query["answer"],
                       "seconds": round(time.monotonic() - began, 2)}
                if kind == "answerable":
                    answer = (query["answer"] or "").lower()
                    cited = " ".join(citation["text"] for citation in response["citations"])
                    evidence = item["evidence"] if isinstance(item["evidence"], list) else [item["evidence"]]
                    row["correct"] = (query["status"] == "answered" and any(fact in answer for fact in item["expect_any"])
                                      and any(line in cited for line in evidence))
                else:
                    row["fabricated"] = query["status"] == "answered"
                results.append(row)
    answerable = [row for row in results if row["kind"] == "answerable"]
    unanswerable = [row for row in results if row["kind"] == "unanswerable"]
    demo = [item["question"] for item in fixture["answerable"] if item.get("demo")]
    absent = [item["question"] for item in fixture["unanswerable"] if item.get("demo_absent")]
    return {
        "repeats": repeats,
        "grounded_correct": f"{sum(row['correct'] for row in answerable)}/{len(answerable)}",
        "answerable_status": {status: sum(row["status"] == status for row in answerable)
                              for status in ("answered", "no_grounding", "failed")},
        "fabricated": f"{sum(row['fabricated'] for row in unanswerable)}/{len(unanswerable)}",
        "unanswerable_reasons": {reason: sum(row["reason"] == reason for row in unanswerable)
                                 for reason in sorted({row["reason"] for row in unanswerable}, key=str)},
        "demo_question_correct": all(row["correct"] for row in answerable if row["question"] in demo),
        "demo_absent_not_answered": all(not row["fabricated"] for row in unanswerable if row["question"] in absent),
        "seconds": _summary([row["seconds"] for row in results]),
        "answer_seconds": _summary([row["seconds"] for row in results if row["status"] == "answered"] or [0.0]),
        "results": results,
    }


async def run(args) -> dict:
    from server.config import load_settings
    from server.rag.embedding import build_embedding_adapter
    from server.rag.reasoning import build_reasoning_adapter
    settings = load_settings()
    fixture = load_fixture(args.fixture)
    embedding = build_embedding_adapter(settings.embedding)
    reasoning = None
    if args.honesty:
        reasoning = build_reasoning_adapter(settings.reasoning)
        began = time.monotonic()
        reasoning.load()
        print(f"reasoning model {settings.reasoning.model} loaded in {time.monotonic() - began:.1f} s", flush=True)
    with tempfile.TemporaryDirectory() as folder:
        db = Database.open(Path(folder) / "calibration.db")
        try:
            meeting_id = seed(db, fixture)
            indexer = TranscriptIndexer(db, embedding, settings.rag)
            await indexer.start()
            await indexer.flush(meeting_id)
            chunks = transcript_chunks.list_for_meeting(db.conn, meeting_id)
            qa = settings.qa if args.threshold is None else replace(settings.qa, min_similarity=args.threshold)
            service = QAService(db, qa, embedding=embedding, reasoning=reasoning, indexer=indexer)
            report = {"fixture": str(args.fixture), "embedding": settings.embedding.model, "chunks": len(chunks),
                      "top_k": qa.top_k, "threshold": await threshold_report(service, meeting_id, fixture)}
            if args.honesty:
                report["reasoning"] = settings.reasoning.model
                report["min_similarity_used"] = qa.min_similarity
                report["honesty"] = await honesty_report(service, meeting_id, fixture, args.repeats)
            await indexer.stop()
        finally:
            db.close()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--fixture", type=Path, default=DEFAULT_FIXTURE)
    parser.add_argument("--honesty", action="store_true", help="also ask every question through the reasoning model")
    parser.add_argument("--threshold", type=float, help="min_similarity for the honesty run (default: [qa] config)")
    parser.add_argument("--repeats", type=int, default=3)
    parser.add_argument("--out", type=Path, help="write the full JSON report here")
    args = parser.parse_args()
    report = asyncio.run(run(args))
    brief = {key: value for key, value in report.items() if key not in ("threshold", "honesty")}
    brief["threshold"] = {key: value for key, value in report["threshold"].items() if key != "questions"}
    if "honesty" in report:
        brief["honesty"] = {key: value for key, value in report["honesty"].items() if key != "results"}
    print(json.dumps(brief, indent=2))
    if args.out:
        args.out.write_text(json.dumps(report, indent=2))
        print(f"full report: {args.out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
