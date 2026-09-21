import re
import uuid

_UUID_V4 = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-4[0-9a-f]{3}-[89ab][0-9a-f]{3}-[0-9a-f]{12}$")


def new_id() -> str:
    """A new UUID v4 as lowercase hyphenated text (docs/data-model.md)."""
    return str(uuid.uuid4())


def is_uuid4(value: object) -> bool:
    return isinstance(value, str) and _UUID_V4.match(value) is not None
