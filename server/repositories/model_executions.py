"""ModelExecution: one row per model invocation, for observability and post-hoc latency debugging."""
from dataclasses import asdict, dataclass

from . import base


@dataclass(frozen=True, slots=True)
class ModelExecution:
    model_execution_id: str
    resource_type: str
    model_identifier: str
    runtime: str
    duration_ms: int
    related_id: str | None
    created_at: str


def insert(conn, execution: ModelExecution) -> ModelExecution:
    base.insert(conn, "ModelExecution", asdict(execution))
    return execution


def list_executions(conn, *, resource_type: str | None = None, limit: int | None = None) -> list[ModelExecution]:
    sql, params = "SELECT * FROM ModelExecution", []
    if resource_type is not None:
        sql += " WHERE resource_type = ?"
        params.append(resource_type)
    sql += " ORDER BY created_at, rowid LIMIT ?"
    params.append(-1 if limit is None else limit)
    return [ModelExecution(**{k: r[k] for k in ModelExecution.__dataclass_fields__}) for r in base.query_all(conn, sql, params)]


def count(conn, resource_type: str | None = None) -> int:
    if resource_type is None:
        return base.query_one(conn, "SELECT COUNT(*) FROM ModelExecution")[0]
    return base.query_one(conn, "SELECT COUNT(*) FROM ModelExecution WHERE resource_type = ?", (resource_type,))[0]
