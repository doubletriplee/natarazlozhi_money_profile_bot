from __future__ import annotations

import argparse
import base64
import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

BACKUP_HEADER = b"MPB1"
BACKUP_AAD = b"money-profile-backup-v1"


def decode_key() -> bytes:
    value = os.environ.get("BACKUP_ENCRYPTION_KEY", "")
    key = base64.b64decode(value.encode(), altchars=b"-_", validate=True)
    if len(key) != 32:
        raise ValueError("BACKUP_ENCRYPTION_KEY must be URL-safe base64 for 32 bytes")
    return key


def database_integrity(path: Path) -> bool:
    connection = sqlite3.connect(path)
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
        return rows == [("ok",)]
    finally:
        connection.close()


def decrypt_backup(source: Path) -> bytes:
    raw = source.read_bytes()
    if raw[:4] != BACKUP_HEADER:
        raise ValueError("unsupported backup format")
    return AESGCM(decode_key()).decrypt(raw[4:16], raw[16:], BACKUP_AAD)


def verify_backup(source: Path) -> None:
    plaintext = decrypt_backup(source)
    with tempfile.TemporaryDirectory(prefix="money-profile-verify-") as temporary:
        restored = Path(temporary) / "restored.sqlite3"
        restored.write_bytes(plaintext)
        if not database_integrity(restored):
            raise ValueError("restored SQLite integrity check failed")


def prune_backups(destination: Path, retention_days: int) -> None:
    if retention_days < 1:
        raise ValueError("retention_days must be positive")
    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    for candidate in destination.glob("money-profile-*.sqlite3.aesgcm"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
        if modified < cutoff:
            candidate.unlink()


def backup(database: Path, destination: Path, retention_days: int) -> Path:
    if not database.is_file():
        raise FileNotFoundError(f"database does not exist: {database}")
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    output = destination / f"money-profile-{stamp}.sqlite3.aesgcm"
    with tempfile.TemporaryDirectory(prefix="money-profile-backup-") as temporary:
        snapshot = Path(temporary) / "snapshot.sqlite3"
        source = sqlite3.connect(database)
        target = sqlite3.connect(snapshot)
        try:
            source.backup(target)
        finally:
            target.close()
            source.close()
        nonce = os.urandom(12)
        ciphertext = AESGCM(decode_key()).encrypt(nonce, snapshot.read_bytes(), BACKUP_AAD)
        handle, temporary_name = tempfile.mkstemp(
            dir=destination,
            prefix=".money-profile-",
            suffix=".tmp",
        )
        encrypted_temporary = Path(temporary_name)
        try:
            with os.fdopen(handle, "wb") as stream:
                stream.write(BACKUP_HEADER + nonce + ciphertext)
                stream.flush()
                os.fsync(stream.fileno())
            encrypted_temporary.chmod(0o600)
            os.replace(encrypted_temporary, output)
        finally:
            encrypted_temporary.unlink(missing_ok=True)

    try:
        verify_backup(output)
    except Exception:
        output.unlink(missing_ok=True)
        raise
    prune_backups(destination, retention_days)
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--retention-days", type=int, default=14)
    arguments = parser.parse_args()
    print(backup(arguments.database, arguments.destination, arguments.retention_days))
