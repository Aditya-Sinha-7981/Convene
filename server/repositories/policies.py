"""Append-only policy document and version rows."""
from dataclasses import asdict
from . import base
from .models import PolicyDocument, PolicyVersion, PolicyChunk

def insert_document(conn, row): base.insert(conn, "PolicyDocument", asdict(row)); return row
def insert_version(conn, row): base.insert(conn, "PolicyVersion", asdict(row)); return row
def insert_chunk(conn, row): base.insert(conn, "PolicyChunk", asdict(row)); return row
def document(conn, policy_id):
    row = base.query_one(conn, "SELECT * FROM PolicyDocument WHERE policy_id = ?", (policy_id,))
    return PolicyDocument.from_row(row) if row else None
def version(conn, version_id):
    row = base.query_one(conn, "SELECT * FROM PolicyVersion WHERE policy_version_id = ?", (version_id,))
    return PolicyVersion.from_row(row) if row else None
def versions(conn, policy_id):
    return [PolicyVersion.from_row(r) for r in base.query_all(conn, "SELECT * FROM PolicyVersion WHERE policy_id=? ORDER BY version_number DESC", (policy_id,))]
def documents(conn):
    return [PolicyDocument.from_row(r) for r in base.query_all(conn, "SELECT * FROM PolicyDocument ORDER BY created_at DESC")]
def chunks(conn, version_id):
    return [PolicyChunk.from_row(r) for r in base.query_all(conn, "SELECT * FROM PolicyChunk WHERE policy_version_id=? ORDER BY chunk_index", (version_id,))]
def current_versions(conn):
    rows = base.query_all(conn, "SELECT v.* FROM PolicyVersion v JOIN (SELECT policy_id, MAX(version_number) n FROM PolicyVersion WHERE status='ready' GROUP BY policy_id) c ON c.policy_id=v.policy_id AND c.n=v.version_number")
    return [PolicyVersion.from_row(r) for r in rows]
def chunk(conn, chunk_id):
    row=base.query_one(conn,"SELECT * FROM PolicyChunk WHERE policy_chunk_id=?",(chunk_id,))
    return PolicyChunk.from_row(row) if row else None
def update_version_status(conn, version_id, **changes): base.update(conn, "PolicyVersion", "policy_version_id", version_id, changes, frozenset({"extracted_text","status","error_code","error_message"}))
def update_chunk(conn, chunk_id, **changes): base.update(conn, "PolicyChunk", "policy_chunk_id", chunk_id, changes, frozenset({"status","error_message"}))
