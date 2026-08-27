from __future__ import annotations

import argparse
import base64
import os
import sqlite3
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def decode_key() -> bytes:
    value = os.environ.get("BACKUP_ENCRYPTION_KEY", "")
    key = base64.urlsafe_b64decode(value.encode())
    if len(key) != 32:
        raise ValueError("BACKUP_ENCRYPTION_KEY must be URL-safe base64 for 32 bytes")
    return key


def backup(database: Path, destination: Path, retention_days: int) -> Path:
    destination.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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
        ciphertext = AESGCM(decode_key()).encrypt(
            nonce, snapshot.read_bytes(), b"money-profile-backup-v1"
        )
        output.write_bytes(b"MPB1" + nonce + ciphertext)
        output.chmod(0o600)

    cutoff = datetime.now(UTC) - timedelta(days=retention_days)
    for candidate in destination.glob("money-profile-*.sqlite3.aesgcm"):
        modified = datetime.fromtimestamp(candidate.stat().st_mtime, UTC)
        if modified < cutoff:
            candidate.unlink()
    return output


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("database", type=Path)
    parser.add_argument("destination", type=Path)
    parser.add_argument("--retention-days", type=int, default=14)
    arguments = parser.parse_args()
    print(backup(arguments.database, arguments.destination, arguments.retention_days))
