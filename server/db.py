"""SQLite connection factory, migration runner, and the transaction/threading model.

Concurrency model (CON-03, Requirement 2)
-----------------------------------------
One SQLite connection per ``Database``, opened with ``check_same_thread=False`` and guarded by a lock, so
every transaction is serialized. The asyncio server calls ``await db.run(fn)``: ``fn`` runs on a dedicated
single worker thread, so a slow or blocked write never stalls the event loop that is receiving audio.
Synchronous code (tests, startup) uses ``with db.transaction() as tx``. Every transaction is
``BEGIN IMMEDIATE``, which takes the write lock up front, so two ``Database`` objects on the same file also
serialize (the loser waits up to the busy timeout, then gets ``DatabaseBusyError``). Each ``run`` is its own
short transaction; there is no long-lived shared transaction, so one device's failing write cannot block or
poison another's.

Audit subscribers are notified only after the transaction commits. On the async path notifications are
queued to the event loop from the worker thread in commit order (``call_soon_threadsafe`` is FIFO), so
subscribers see events in ``seq`` order and before the awaiting caller resumes.
"""
import asyncio
import logging
import re
import sqlite3
import threading
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from typing import Callable, Iterator

from .errors import (
    DatabaseBusyError,
    DatabaseOpenError,
    MigrationError,
    SchemaVersionError,
    StorageError,
    translate_sqlite_error,
)

log = logging.getLogger("convene.db")

MIGRATIONS_DIR = Path(__file__).resolve().parent / "migrations"
DEFAULT_BUSY_TIMEOUT_MS = 5000
_MIGRATION_NAME = re.compile(r"^(\d{4})_[a-z0-9_]+\.sql$")


# --- migrations -----------------------------------------------------------------------------


def discover_migrations(directory: Path = MIGRATIONS_DIR) -> list[tuple[int, Path]]:
    """Numbered SQL files in order. Numbers must run 1, 2, 3, ... with no gap or duplicate."""
    found = []
    for path in sorted(Path(directory).glob("*.sql")):
        match = _MIGRATION_NAME.match(path.name)
        if not match:
            raise MigrationError(f"migration file name must look like 0001_name.sql: {path.name}")
        found.append((int(match.group(1)), path))
    for expected, (number, path) in enumerate(found, start=1):
        if number != expected:
            raise MigrationError(f"migration numbering must be contiguous from 0001; got {path.name} at position {expected}")
    return found


def migrate(conn: sqlite3.Connection, directory: Path = MIGRATIONS_DIR) -> int:
    """Apply pending migrations, each in its own transaction, tracked by ``PRAGMA user_version``.

    Idempotent. A failing migration is rolled back completely and leaves the database at its previous
    version. Returns the version after the run.
    """
    known = discover_migrations(directory)
    latest = known[-1][0] if known else 0
    current = conn.execute("PRAGMA user_version").fetchone()[0]
    if current > latest:
        raise SchemaVersionError(
            f"database schema version {current} is newer than this code understands ({latest}); "
            "refusing to start rather than risk corrupting it"
        )
    for version, path in known:
        if version <= current:
            continue
        script = path.read_text(encoding="utf-8")
        try:
            conn.executescript(f"BEGIN IMMEDIATE;\n{script}\nPRAGMA user_version = {version};\nCOMMIT;")
        except sqlite3.Error as exc:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise MigrationError(f"migration {path.name} failed and was rolled back: {exc}") from exc
    return latest


# --- connection -----------------------------------------------------------------------------


def connect(path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS) -> sqlite3.Connection:
    """Open a connection: foreign keys on, WAL, busy timeout, rows by column name, manual transactions."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    try:
        conn = sqlite3.connect(str(path), isolation_level=None, check_same_thread=False,
                               timeout=busy_timeout_ms / 1000)
        conn.row_factory = sqlite3.Row
        conn.execute(f"PRAGMA busy_timeout = {int(busy_timeout_ms)}")
        conn.execute("PRAGMA foreign_keys = ON")
        conn.execute("PRAGMA journal_mode = WAL")
        # WAL + NORMAL survives an application crash; only an OS crash or power loss can lose the
        # last committed transaction. Acceptable for a single-laptop demo.
        conn.execute("PRAGMA synchronous = NORMAL")
        # vec0 tables are schema objects: load the extension before migrations or any query can touch one.
        # Import here gives a clear startup failure when the declared runtime dependency is missing.
        try:
            import sqlite_vec
            conn.enable_load_extension(True)
            sqlite_vec.load(conn)
            conn.enable_load_extension(False)
        except (ImportError, sqlite3.Error) as exc:
            conn.close()
            raise DatabaseOpenError("sqlite-vec is required for transcript indexing; install requirements.txt") from exc
        conn.execute("PRAGMA user_version").fetchone()  # reads the file header; raises on a non-database
    except sqlite3.Error as exc:
        raise DatabaseOpenError(f"cannot open {path} as a SQLite database: {exc}") from exc
    return conn


class Tx:
    """A transaction in progress. Passed to repositories and to ``audit.emit``.

    ``events`` collects audit events written in this transaction; they are delivered to subscribers
    only after it commits.
    """

    __slots__ = ("conn", "events")

    def __init__(self, conn: sqlite3.Connection):
        self.conn = conn
        self.events: list = []


class Database:
    def __init__(self, conn: sqlite3.Connection):
        self._conn = conn
        self._lock = threading.RLock()
        self._executor = ThreadPoolExecutor(max_workers=1, thread_name_prefix="convene-db")
        self._subscribers: list[Callable] = []

    @classmethod
    def open(cls, path: str | Path, *, busy_timeout_ms: int = DEFAULT_BUSY_TIMEOUT_MS,
             migrations_dir: Path = MIGRATIONS_DIR) -> "Database":
        """Connect and bring the schema up to date. Raises before returning if the file is unusable."""
        conn = connect(path, busy_timeout_ms=busy_timeout_ms)
        try:
            migrate(conn, migrations_dir)
        except Exception:
            conn.close()
            raise
        return cls(conn)

    # -- audit subscribers ------------------------------------------------------------------

    def subscribe(self, callback: Callable) -> None:
        """Register ``callback(event: AuditEvent)``, called after each commit that emitted events."""
        self._subscribers.append(callback)

    def unsubscribe(self, callback: Callable) -> None:
        if callback in self._subscribers:
            self._subscribers.remove(callback)

    def _deliver(self, events: list) -> None:
        for event in events:
            for callback in list(self._subscribers):
                try:
                    callback(event)
                except Exception:
                    log.exception("audit subscriber failed for %s seq=%s", event.event_type, event.seq)

    # -- transactions -----------------------------------------------------------------------

    def _begin(self) -> Tx:
        try:
            self._conn.execute("BEGIN IMMEDIATE")
        except sqlite3.Error as exc:
            raise translate_sqlite_error(exc) from exc
        return Tx(self._conn)

    def _end(self, commit: bool) -> None:
        try:
            self._conn.execute("COMMIT" if commit else "ROLLBACK")
        except sqlite3.Error as exc:
            if self._conn.in_transaction:
                self._conn.execute("ROLLBACK")
            raise translate_sqlite_error(exc) from exc

    @contextmanager
    def transaction(self) -> Iterator[Tx]:
        """Synchronous transaction: commit on success, roll back on any exception, then notify subscribers."""
        with self._lock:
            tx = self._begin()
            try:
                yield tx
            except BaseException as exc:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise translate_sqlite_error(exc) from exc
                raise
            self._end(commit=True)
        self._deliver(tx.events)

    def _run_sync(self, fn: Callable[[Tx], object], loop: asyncio.AbstractEventLoop | None):
        with self._lock:
            tx = self._begin()
            try:
                result = fn(tx)
            except BaseException as exc:
                if self._conn.in_transaction:
                    self._conn.execute("ROLLBACK")
                if isinstance(exc, sqlite3.Error):
                    raise translate_sqlite_error(exc) from exc
                raise
            self._end(commit=True)
            if tx.events:
                if loop is not None:
                    loop.call_soon_threadsafe(self._deliver, tx.events)  # FIFO => commit order
                else:
                    self._deliver(tx.events)
        return result

    async def run(self, fn: Callable[[Tx], object]):
        """Run ``fn(tx)`` in one transaction on the database worker thread and return its result.

        Never blocks the event loop. If ``fn`` raises, the transaction is rolled back and the exception
        (a typed ``StorageError`` for database errors) propagates to the caller.
        """
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._executor, self._run_sync, fn, loop)

    def close(self) -> None:
        self._executor.shutdown(wait=True)
        with self._lock:
            self._conn.close()

    @property
    def conn(self) -> sqlite3.Connection:
        """The raw connection, for read-only inspection in tests and scripts. Use ``transaction()`` to write."""
        return self._conn


__all__ = ["Database", "Tx", "connect", "migrate", "discover_migrations", "MIGRATIONS_DIR",
           "DatabaseBusyError", "StorageError"]
