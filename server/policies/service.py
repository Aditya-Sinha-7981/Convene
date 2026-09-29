"""Policy upload and asynchronous local extraction/indexing. Versions are never edited."""
from __future__ import annotations
import asyncio
import hashlib
import os
import time
from dataclasses import asdict
from pathlib import Path
from ..audit import emit
from ..ids import new_id
from ..repositories import model_executions, policies
from ..repositories.models import PolicyChunk, PolicyDocument, PolicyVersion
from ..repositories.model_executions import ModelExecution
from ..timeutil import utc_now
from ..rag.chunker import chunk_text
from ..rag.vector_store import VectorStore
from .extract import ExtractionError, extract, media_type

class PolicyService:
    def __init__(self, db, settings, adapter=None, priority=None):
        self.db, self.settings, self.adapter, self.priority = db, settings, adapter, priority
        self.store = VectorStore(adapter.dimension, adapter.model_identifier) if adapter else None
        self._tasks=set()
        self._upload_lock = asyncio.Lock()
        self._failures: dict[str, int] = {}

    def _path(self, policy_id, version_number, digest):
        return (self.settings.policies_dir or self.settings.root / "data/policies") / policy_id / f"v{version_number}-{digest}.bin"

    async def start(self):
        """Resume interrupted pending work after a restart; originals are the recovery source."""
        pending=await self.db.run(lambda tx: [v for d in policies.documents(tx.conn) for v in policies.versions(tx.conn,d.policy_id) if v.status=="pending"])
        for version in pending:
            base=self.settings.policies_dir or self.settings.root / "data/policies"; path=base/version.storage_path
            if path.is_file(): self._schedule(version.policy_version_id, path.read_bytes())
            else: await self.db.run(lambda tx, version=version: policies.update_version_status(tx.conn,version.policy_version_id,status="failed",error_code="storage_missing",error_message="the retained original file is missing"))

    def _schedule(self, version_id, data):
        task=asyncio.create_task(self._process(version_id,data)); self._tasks.add(task); task.add_done_callback(self._tasks.discard)

    async def upload(self, title, tags, filename, data, policy_id=None):
        if not data: raise ValueError("file is empty")
        if len(data) > self.settings.policies.max_upload_bytes: raise OverflowError
        kind = media_type(data); now=utc_now(); digest=hashlib.sha256(data).hexdigest()
        async with self._upload_lock:
            def prepare(tx):
                document = policies.document(tx.conn, policy_id) if policy_id else None
                if policy_id and not document: raise KeyError(policy_id)
                if document is None:
                    document=PolicyDocument(new_id(), title, __import__('json').dumps(tags), now)
                existing=policies.versions(tx.conn, document.policy_id)
                number=(existing[0].version_number + 1) if existing else 1
                path=self._path(document.policy_id, number, digest)
                base = self.settings.policies_dir or self.settings.root / "data/policies"
                return document, PolicyVersion(new_id(),document.policy_id,number,str(path.relative_to(base)),digest,filename,kind,len(data),None,"pending",None,None,now), path
            document, version, path=await self.db.run(prepare)
            path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_suffix(".tmp")
            try:
                await asyncio.get_running_loop().run_in_executor(None, lambda: (tmp.write_bytes(data), os.replace(tmp,path)))
            except Exception:
                if tmp.exists(): tmp.unlink(missing_ok=True)
                raise
            try:
                def create(tx):
                    if policies.document(tx.conn, document.policy_id) is None:
                        policies.insert_document(tx.conn, document)
                        emit(tx, "policy_created", "policy", {"policy_id":document.policy_id,"title":document.title,"tag_count":len(tags)}, meeting_id=None)
                    policies.insert_version(tx.conn,version)
                    emit(tx,"policy_version_added","policy",{"policy_id":document.policy_id,"policy_version_id":version.policy_version_id,"version_number":version.version_number,"byte_size":len(data),"content_hash":digest},meeting_id=None)
                await self.db.run(create)
            except Exception:
                path.unlink(missing_ok=True)
                raise
        self._schedule(version.policy_version_id, data)
        return {"policy":asdict(document),"version":asdict(version)}

    async def retry(self, version_id):
        version=await self.db.run(lambda tx: policies.version(tx.conn,version_id))
        if not version: raise KeyError(version_id)
        base=self.settings.policies_dir or self.settings.root / "data/policies"; path=base/version.storage_path
        if not path.is_file(): raise FileNotFoundError(path)
        await self.db.run(lambda tx: policies.update_version_status(tx.conn,version_id,status="pending",error_code=None,error_message=None))
        self._schedule(version_id,path.read_bytes())
        version = await self.db.run(lambda tx: policies.version(tx.conn, version_id))
        return version

    async def _process(self, version_id, data):
        try:
            version=await self.db.run(lambda tx: policies.version(tx.conn,version_id)); text=await asyncio.get_running_loop().run_in_executor(None,extract,data,version.media_type)
            await self._index(version,text)
        except ExtractionError as exc:
            await self.db.run(lambda tx: (policies.update_version_status(tx.conn,version_id,status="failed",error_code=exc.code,error_message=exc.message), emit(tx,"policy_extract_failed","policy",{"policy_version_id":version_id,"error_code":exc.code},meeting_id=None)))
        except Exception as exc:
            await self.db.run(lambda tx: (policies.update_version_status(tx.conn,version_id,status="failed",error_code="index_failed",error_message=str(exc)[:300]), emit(tx,"policy_index_failed","policy",{"policy_version_id":version_id,"error_code":"index_failed"},meeting_id=None)))
            self._retry_later(version_id, data)

    def _retry_later(self, version_id, data):
        failures = self._failures[version_id] = self._failures.get(version_id, 0) + 1
        delay = min(self.settings.rag.retry_delay_s * (2 ** (failures - 1)), self.settings.rag.retry_max_delay_s)
        async def retry():
            await asyncio.sleep(delay)
            version = await self.db.run(lambda tx: policies.version(tx.conn, version_id))
            if version is not None and version.status == "failed" and version.error_code == "index_failed":
                await self.db.run(lambda tx: policies.update_version_status(tx.conn, version_id, status="pending", error_code=None, error_message=None))
                self._schedule(version_id, data)
        task=asyncio.create_task(retry()); self._tasks.add(task); task.add_done_callback(self._tasks.discard)

    async def _index(self, version, text):
        if not self.adapter: raise RuntimeError("embedding model unavailable")
        document=await self.db.run(lambda tx: policies.document(tx.conn,version.policy_id))
        chunks=await asyncio.get_running_loop().run_in_executor(None,lambda: chunk_text(text, f"Policy: {document.title} (version {version.version_number}, {version.uploaded_at[:10]})", target_tokens=self.settings.rag.target_tokens, hard_max_tokens=self.settings.rag.hard_max_tokens, count_tokens=self.adapter.count_tokens))
        rows=[PolicyChunk(new_id(),version.policy_version_id,i,item,"pending",None,utc_now()) for i,item in enumerate(chunks)]
        await self.db.run(lambda tx: policies.replace_chunks(tx.conn, version.policy_version_id, rows))
        if self.priority: await self.priority.wait_for_turn("embedding")
        vectors=await asyncio.get_running_loop().run_in_executor(None,self.adapter.embed,[r.text for r in rows])
        began = time.monotonic()
        def commit(tx):
            self.store.guard_model(tx.conn)
            for row, vector in zip(rows,vectors): self.store.upsert_policy(tx.conn,row.policy_chunk_id,row.policy_version_id,vector); policies.update_chunk(tx.conn,row.policy_chunk_id,status="ready",error_message=None)
            model_executions.insert(tx.conn, ModelExecution(new_id(), "embedding", self.adapter.model_identifier,
                                    self.adapter.runtime, round((time.monotonic()-began)*1000), rows[0].policy_chunk_id if rows else version.policy_version_id, utc_now()))
            policies.update_version_status(tx.conn,version.policy_version_id,extracted_text=text,status="ready",error_code=None,error_message=None)
            emit(tx,"policy_indexed","policy",{"policy_id":version.policy_id,"policy_version_id":version.policy_version_id,"version_number":version.version_number,"chunk_count":len(rows)},meeting_id=None)
        await self.db.run(commit)
        self._failures.pop(version.policy_version_id, None)
