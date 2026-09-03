from __future__ import annotations

import argparse
import os
import tempfile
from pathlib import Path

from backup import database_integrity, decrypt_backup


def restore(source: Path, destination: Path) -> None:
    plaintext = decrypt_backup(source)
    if destination.exists():
        raise FileExistsError("restore destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    handle, temporary_name = tempfile.mkstemp(
        dir=destination.parent,
        prefix=f".{destination.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(handle, "wb") as stream:
            stream.write(plaintext)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o600)
        if not database_integrity(temporary):
            raise ValueError("restored SQLite integrity check failed")
        os.link(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    restore(arguments.source, arguments.destination)
