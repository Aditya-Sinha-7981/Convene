from datetime import datetime, timezone


def utc_now() -> str:
    """Current time as ISO 8601 UTC with millisecond precision and a Z suffix (docs/data-model.md)."""
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")
