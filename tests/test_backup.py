from __future__ import annotations

import base64
import os
import secrets
import sqlite3
import subprocess
import sys
from pathlib import Path


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
