from __future__ import annotations

import argparse
import logging
import os
import time
from datetime import UTC, datetime
from pathlib import Path

from backup import backup

from money_profile_bot.backup_status import backup_status_is_healthy, write_backup_status

logger = logging.getLogger("money_profile_bot.backup")


def database_path(value: str) -> Path:
    prefix = "sqlite+aiosqlite:///"
    if not value.startswith(prefix):
        raise ValueError("backup worker requires a sqlite+aiosqlite database URL")
    return Path(value.removeprefix(prefix))


def positive_integer(name: str, default: int) -> int:
    raw = os.environ.get(name, str(default))
    value = int(raw)
    if value < 1:
        raise ValueError(f"{name} must be positive")
    return value


def run_once(database: Path, destination: Path, status_path: Path, retention_days: int) -> Path:
    created = backup(database, destination, retention_days)
    completed_at = datetime.now(UTC)
    write_backup_status(
        status_path,
        status="ok",
        completed_at=completed_at,
        backup_name=created.name,
        backup_size=created.stat().st_size,
    )
    logger.info("encrypted backup created and restored integrity check passed")
    return created


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--check", action="store_true")
    arguments = parser.parse_args()

    destination = Path(os.environ.get("BACKUP_DESTINATION", "/data/backups"))
    status_path = Path(os.environ.get("BACKUP_STATUS_PATH", str(destination / "status.json")))
    max_age_hours = positive_integer("BACKUP_MAX_AGE_HOURS", 8)
    if arguments.check:
        return (
            0
            if backup_status_is_healthy(
                status_path,
                max_age_hours=max_age_hours,
            )
            else 1
        )

    database = database_path(os.environ.get("DATABASE_URL", ""))
    retention_days = positive_integer("BACKUP_RETENTION_DAYS", 14)
    interval_hours = positive_integer("BACKUP_INTERVAL_HOURS", 6)
    if max_age_hours <= interval_hours:
        raise ValueError("BACKUP_MAX_AGE_HOURS must be greater than BACKUP_INTERVAL_HOURS")

    while True:
        try:
            run_once(database, destination, status_path, retention_days)
        except Exception:
            logger.exception("encrypted backup cycle failed")
            try:
                write_backup_status(status_path, status="error")
            except Exception:
                logger.exception("could not publish backup failure status")
            if arguments.once:
                return 1
            time.sleep(60)
            continue
        if arguments.once:
            return 0
        time.sleep(interval_hours * 60 * 60)


if __name__ == "__main__":
    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO").upper())
    raise SystemExit(main())
