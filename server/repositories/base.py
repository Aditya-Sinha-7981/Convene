"""Shared SQL helpers for the repositories. Every sqlite3 error leaves here as a typed StorageError."""
import sqlite3
from typing import Iterable

from ..errors import NotFoundError, StorageError, ValidationError, translate_sqlite_error


def _param(value):
    return int(value) if isinstance(value, bool) else value


def execute(conn: sqlite3.Connection, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
    try:
        return conn.execute(sql, tuple(_param(p) for p in params))
    except sqlite3.Error as exc:
        raise translate_sqlite_error(exc) from exc


def query_one(conn, sql: str, params: Iterable = ()):
    return execute(conn, sql, params).fetchone()


def query_all(conn, sql: str, params: Iterable = ()):
    return execute(conn, sql, params).fetchall()


def insert(conn, table: str, values: dict) -> None:
    columns = ", ".join(values)
    marks = ", ".join("?" for _ in values)
    execute(conn, f"INSERT INTO {table} ({columns}) VALUES ({marks})", values.values())


def update(conn, table: str, key_column: str, key: str, changes: dict, allowed: frozenset,
           not_found: type[NotFoundError] = NotFoundError) -> None:
    """UPDATE the listed columns of one row. Column names are checked against ``allowed``."""
    unknown = set(changes) - allowed
    if unknown:
        raise ValidationError(f"{table}: cannot update {sorted(unknown)}; allowed: {sorted(allowed)}")
    if not changes:
        return
    assignments = ", ".join(f"{column} = ?" for column in changes)
    cursor = execute(conn, f"UPDATE {table} SET {assignments} WHERE {key_column} = ?",
                     [*changes.values(), key])
    if cursor.rowcount == 0:
        raise not_found(f"{table} {key} does not exist")


__all__ = ["execute", "query_one", "query_all", "insert", "update", "StorageError"]
