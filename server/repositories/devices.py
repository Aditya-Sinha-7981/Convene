from dataclasses import asdict
from typing import Iterable

from ..errors import DeviceNotFoundError
from . import base
from .models import Device

UPDATABLE = frozenset({"status", "reconnect_count", "user_agent", "declared_speaker_count"})


def create(conn, device: Device) -> Device:
    base.insert(conn, "Device", asdict(device))
    return device


def get(conn, device_id: str) -> Device | None:
    row = base.query_one(conn, "SELECT * FROM Device WHERE device_id = ?", (device_id,))
    return Device.from_row(row) if row else None


def require(conn, device_id: str) -> Device:
    device = get(conn, device_id)
    if device is None:
        raise DeviceNotFoundError(f"device {device_id} does not exist")
    return device


def list_for_meeting(conn, meeting_id: str) -> list[Device]:
    rows = base.query_all(conn, "SELECT * FROM Device WHERE meeting_id = ? ORDER BY joined_at, device_id",
                          (meeting_id,))
    return [Device.from_row(r) for r in rows]


def list_by_status_in_open_meetings(conn, statuses: Iterable[str]) -> list[Device]:
    """Devices with one of ``statuses`` whose meeting has not ended."""
    statuses = list(statuses)
    marks = ", ".join("?" for _ in statuses)
    rows = base.query_all(
        conn,
        f"SELECT d.* FROM Device d JOIN Meeting m ON m.meeting_id = d.meeting_id "
        f"WHERE m.status != 'ended' AND d.status IN ({marks}) ORDER BY d.joined_at, d.device_id",
        statuses)
    return [Device.from_row(r) for r in rows]


def update(conn, device_id: str, /, **changes) -> Device:
    base.update(conn, "Device", "device_id", device_id, changes, UPDATABLE, DeviceNotFoundError)
    return require(conn, device_id)
