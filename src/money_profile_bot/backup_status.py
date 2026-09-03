from __future__ import annotations

import json
import os
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any


def _utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def write_backup_status(
    path: Path,
    *,
    status: str,
    completed_at: datetime | None = None,
    backup_name: str | None = None,
    backup_size: int | None = None,
) -> None:
    """Atomically publish a minimal status without database or user data."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload: dict[str, Any] = {
        "version": 1,
        "status": status,
        "checked_at": datetime.now(UTC).isoformat(),
    }
    if completed_at is not None:
        payload["completed_at"] = _utc(completed_at).isoformat()
    if backup_name is not None:
        payload["backup_name"] = backup_name
    if backup_size is not None:
        payload["backup_size"] = backup_size
    handle, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as stream:
            json.dump(payload, stream, ensure_ascii=True, separators=(",", ":"))
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def backup_status_is_healthy(
    path: Path,
    *,
    max_age_hours: int,
    now: datetime | None = None,
) -> bool:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        if payload.get("version") != 1 or payload.get("status") != "ok":
            return False
        completed_at = datetime.fromisoformat(str(payload["completed_at"]))
        if completed_at.tzinfo is None:
            return False
        backup_name = str(payload["backup_name"])
        if Path(backup_name).name != backup_name:
            return False
        backup_size = int(payload["backup_size"])
        backup_path = path.parent / backup_name
        if (
            backup_size <= 0
            or not backup_path.is_file()
            or backup_path.stat().st_size != backup_size
        ):
            return False
        age = _utc(now or datetime.now(UTC)) - _utc(completed_at)
        return -timedelta(minutes=5) <= age <= timedelta(hours=max_age_hours)
    except (KeyError, OSError, TypeError, ValueError, json.JSONDecodeError):
        return False
