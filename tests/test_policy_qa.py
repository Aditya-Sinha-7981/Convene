"""CON-17 policy and mixed history Q&A stays explicit and cites stored policy rows."""
import asyncio
import io

import pytest
from docx import Document

from server.config import PoliciesConfig, Settings
from server.db import Database
from server.policies.service import PolicyService
from server.rag.qa import QAService
from server.rag.reasoning import FakeReasoningAdapter
from tests.support.qa import QA, TopicEmbedding


def _docx(text):
    doc=Document(); doc.add_paragraph(text); out=io.BytesIO(); doc.save(out); return out.getvalue()


@pytest.mark.asyncio
async def test_policy_scope_answers_with_a_typed_policy_citation(tmp_path):
    settings=Settings(root=tmp_path,database_path=tmp_path/"db.sqlite",exports_dir=tmp_path/"exports",policies_dir=tmp_path/"policies")
    db=Database.open(settings.database_path); embedding=TopicEmbedding()
    ingestion=PolicyService(db,settings,embedding)
    await ingestion.upload("Travel rules",["travel"],"travel.docx",_docx("The travel budget needs director approval."))
    await asyncio.gather(*ingestion._tasks)
    reasoning=FakeReasoningAdapter(lambda _messages: "The Travel rules policy requires director approval.")
    qa=QAService(db,QA,embedding=embedding,reasoning=reasoning,policies_config=PoliciesConfig(min_similarity=.6,per_source_cap=2))
    result=await qa.ask_history({"mode":"history","sources":"policies","question":"Who approves the travel budget?"})
    assert result["query"]["status"] == "answered"
    [citation]=result["citations"]
    assert citation["source_type"] == "policy"
    assert citation["policy_title"] == "Travel rules" and citation["version_number"] == 1
    assert "Policy: Travel rules (version 1" in reasoning.calls[0][1]["content"]
    db.close()


@pytest.mark.asyncio
async def test_policy_scope_does_not_call_model_below_the_policy_threshold(tmp_path):
    settings=Settings(root=tmp_path,database_path=tmp_path/"db.sqlite",exports_dir=tmp_path/"exports",policies_dir=tmp_path/"policies")
    db=Database.open(settings.database_path); embedding=TopicEmbedding(); ingestion=PolicyService(db,settings,embedding)
    await ingestion.upload("Travel rules",[],"travel.docx",_docx("The travel budget needs approval.")); await asyncio.gather(*ingestion._tasks)
    reasoning=FakeReasoningAdapter()
    qa=QAService(db,QA,embedding=embedding,reasoning=reasoning,policies_config=PoliciesConfig(min_similarity=.6,per_source_cap=2))
    result=await qa.ask_history({"mode":"history","sources":"policies","question":"What is the hotel address?"})
    assert result["query"]["status"] == "no_grounding" and result["reason"] == "no_relevant_evidence"
    assert reasoning.calls == []
    db.close()
