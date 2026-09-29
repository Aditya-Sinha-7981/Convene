#!/usr/bin/env python3
"""Remove exactly the synthetic CON-17 meetings named by a seed manifest."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from server import registry
from server.db import Database

DEFAULT_MANIFEST = ROOT / "data" / "con17-policy-demo-manifest.json"


def remove(manifest_path: Path) -> int:
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    database_path = Path(manifest["database"])
    if not database_path.is_file():
        raise SystemExit(f"fixture database does not exist: {database_path}")
    database = Database.open(database_path)
    removed = 0
    try:
        for row in manifest.get("meetings", []):
            meeting_id = row["meeting_id"]
            with database.transaction() as tx:
                if tx.conn.execute("SELECT 1 FROM Meeting WHERE meeting_id = ?", (meeting_id,)).fetchone() is None:
                    continue
                registry.erase_meeting(tx, meeting_id)
                # erase_meeting leaves one audit record for normal product deletion. This fixture cleanup removes
                # that record too, so the database is exactly as it was before the fixture was seeded.
                tx.conn.execute("DELETE FROM AuditEvent WHERE event_type = 'meeting_deleted' "
                                "AND json_extract(payload, '$.deleted_meeting_id') = ?", (meeting_id,))
                removed += 1
    finally:
        database.close()
    for value in manifest.get("files", []):
        path = Path(value)
        if path.is_file():
            path.unlink()
    input_dir = manifest_path.parent / "con17-policy-demo-inputs"
    if input_dir.is_dir() and not any(input_dir.iterdir()):
        input_dir.rmdir()
    config = Path(manifest.get("config", ""))
    if config.is_file():
        config.unlink()
    manifest_path.unlink()
    return removed


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, default=DEFAULT_MANIFEST)
    args = parser.parse_args()
    if not args.manifest.is_file():
        raise SystemExit(f"manifest not found: {args.manifest}; refusing to delete anything")
    print(f"Removed {remove(args.manifest)} recorded synthetic meetings.")


if __name__ == "__main__":
    main()
