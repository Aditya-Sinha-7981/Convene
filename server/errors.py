"""Typed storage errors. API code maps these to the HTTP error codes in docs/api.md."""
import sqlite3


class StorageError(Exception):
    """Base class for every error raised by the persistence layer."""


class ValidationError(StorageError, ValueError):
    """Input rejected before any write (docs/api.md `invalid_request`)."""


class NotFoundError(StorageError):
    """A row that the caller required does not exist."""


class MeetingNotFoundError(NotFoundError):
    """`meeting_not_found`."""


class DeviceNotFoundError(NotFoundError):
    """`device_not_found`."""


class UtteranceNotFoundError(NotFoundError):
    """`utterance_not_found`."""


class ParticipantNotFoundError(NotFoundError):
    """`participant_not_found`."""


class AmbiguousDisplayNameError(StorageError):
    """`ambiguous_display_name`."""


class MeetingEndedError(StorageError):
    """`meeting_ended`: the operation needs a meeting that has not ended."""


class SummaryNotFoundError(NotFoundError):
    """`summary_not_found`: no summary attempt exists for this meeting."""


class SummaryInProgressError(StorageError):
    """`summary_in_progress`: an attempt for this meeting is already running."""


class TranscriptEmptyError(StorageError):
    """`transcript_empty`: the meeting has no utterances to summarize."""


class DeviceConflictError(StorageError):
    """`device_conflict`: the device_id belongs to another meeting, or `is_shared` differs."""


class ConstraintError(StorageError):
    """A CHECK, FOREIGN KEY, UNIQUE or NOT NULL constraint rejected the write. Nothing was written."""


class DatabaseBusyError(StorageError):
    """The database stayed locked past the busy timeout."""


class DatabaseOpenError(StorageError):
    """The file could not be opened as a SQLite database (corrupt, not a database, unreadable)."""


class MigrationError(StorageError):
    """A migration failed or the migration files are malformed. The database stays at its previous version."""


class SchemaVersionError(MigrationError):
    """The database `user_version` is newer than any migration this code knows."""


class AuditError(StorageError):
    """Base class for audit write-path rejections."""


class UnknownEventTypeError(AuditError):
    """`event_type` is not in the catalog in docs/data-model.md."""


class InvalidPayloadError(AuditError):
    """Payload is not a JSON object with exactly the catalogued keys."""


def translate_sqlite_error(exc: sqlite3.Error) -> StorageError:
    """Map a raw sqlite3 error to a typed storage error."""
    if isinstance(exc, sqlite3.IntegrityError):
        return ConstraintError(str(exc))
    message = str(exc).lower()
    if isinstance(exc, sqlite3.OperationalError) and ("locked" in message or "busy" in message):
        return DatabaseBusyError(str(exc))
    return StorageError(str(exc))
