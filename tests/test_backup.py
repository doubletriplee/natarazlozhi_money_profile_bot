from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from money_profile_bot.backup_status import backup_status_is_healthy, write_backup_status


def backup_environment() -> dict[str, str]:
    return {
        **os.environ,
        "BACKUP_ENCRYPTION_KEY": base64.urlsafe_b64encode(secrets.token_bytes(32)).decode(),
    }


def create_database(path: Path) -> None:
    connection = sqlite3.connect(path)
    connection.execute("CREATE TABLE sample (value TEXT)")
    connection.execute("INSERT INTO sample VALUES ('encrypted backup works')")
    connection.commit()
    connection.close()


def test_encrypted_backup_can_be_restored(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    source = tmp_path / "source.sqlite3"
    backups = tmp_path / "backups"
    restored = tmp_path / "restored.sqlite3"
    create_database(source)
    environment = backup_environment()
    result = subprocess.run(
        [sys.executable, str(root / "scripts/backup.py"), str(source), str(backups)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    encrypted = Path(result.stdout.strip())
    assert encrypted.read_bytes().startswith(b"MPB1")
    assert b"encrypted backup works" not in encrypted.read_bytes()
    subprocess.run(
        [sys.executable, str(root / "scripts/restore_backup.py"), str(encrypted), str(restored)],
        check=True,
        env=environment,
    )
    connection = sqlite3.connect(restored)
    try:
        assert (
            connection.execute("SELECT value FROM sample").fetchone()[0] == "encrypted backup works"
        )
    finally:
        connection.close()


def test_tampered_backup_is_rejected(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    source = tmp_path / "source.sqlite3"
    backups = tmp_path / "backups"
    restored = tmp_path / "restored.sqlite3"
    create_database(source)
    environment = backup_environment()
    result = subprocess.run(
        [sys.executable, str(root / "scripts/backup.py"), str(source), str(backups)],
        check=True,
        capture_output=True,
        text=True,
        env=environment,
    )
    encrypted = Path(result.stdout.strip())
    raw = bytearray(encrypted.read_bytes())
    raw[-1] ^= 1
    encrypted.write_bytes(raw)
    failed = subprocess.run(
        [sys.executable, str(root / "scripts/restore_backup.py"), str(encrypted), str(restored)],
        env=environment,
    )
    assert failed.returncode != 0
    assert not restored.exists()


def test_authenticated_but_invalid_sqlite_restore_is_rejected(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    encrypted = tmp_path / "invalid.sqlite3.aesgcm"
    restored = tmp_path / "restored.sqlite3"
    environment = backup_environment()
    key = base64.urlsafe_b64decode(environment["BACKUP_ENCRYPTION_KEY"])
    nonce = secrets.token_bytes(12)
    ciphertext = AESGCM(key).encrypt(nonce, b"not a sqlite database", b"money-profile-backup-v1")
    encrypted.write_bytes(b"MPB1" + nonce + ciphertext)

    failed = subprocess.run(
        [sys.executable, str(root / "scripts/restore_backup.py"), str(encrypted), str(restored)],
        env=environment,
    )
    assert failed.returncode != 0
    assert not restored.exists()


def test_backup_worker_creates_verified_backup_and_healthy_status(tmp_path: Path) -> None:
    root = Path(__file__).parents[1]
    source = tmp_path / "source.sqlite3"
    backups = tmp_path / "backups"
    status = backups / "status.json"
    create_database(source)
    environment = backup_environment() | {
        "DATABASE_URL": f"sqlite+aiosqlite:///{source}",
        "BACKUP_DESTINATION": str(backups),
        "BACKUP_STATUS_PATH": str(status),
        "BACKUP_RETENTION_DAYS": "14",
        "BACKUP_INTERVAL_HOURS": "6",
        "BACKUP_MAX_AGE_HOURS": "8",
    }

    subprocess.run(
        [sys.executable, str(root / "scripts/backup_worker.py"), "--once"],
        check=True,
        env=environment,
    )
    subprocess.run(
        [sys.executable, str(root / "scripts/backup_worker.py"), "--check"],
        check=True,
        env=environment,
    )
    assert backup_status_is_healthy(status, max_age_hours=8)
    assert len(list(backups.glob("money-profile-*.sqlite3.aesgcm"))) == 1


def test_backup_status_rejects_stale_missing_or_failed_backup(tmp_path: Path) -> None:
    backups = tmp_path / "backups"
    backups.mkdir()
    encrypted = backups / "money-profile-test.sqlite3.aesgcm"
    encrypted.write_bytes(b"encrypted")
    status = backups / "status.json"
    now = datetime.now(UTC)
    write_backup_status(
        status,
        status="ok",
        completed_at=now - timedelta(hours=9),
        backup_name=encrypted.name,
        backup_size=encrypted.stat().st_size,
    )
    assert not backup_status_is_healthy(status, max_age_hours=8, now=now)

    write_backup_status(
        status,
        status="error",
    )
    assert not backup_status_is_healthy(status, max_age_hours=8, now=now)

    write_backup_status(
        status,
        status="ok",
        completed_at=now,
        backup_name="missing.sqlite3.aesgcm",
        backup_size=10,
    )
    assert not backup_status_is_healthy(status, max_age_hours=8, now=now)
