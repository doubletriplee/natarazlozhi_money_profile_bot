from __future__ import annotations

import argparse
from pathlib import Path

from backup import decode_key
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def restore(source: Path, destination: Path) -> None:
    raw = source.read_bytes()
    if raw[:4] != b"MPB1":
        raise ValueError("unsupported backup format")
    plaintext = AESGCM(decode_key()).decrypt(raw[4:16], raw[16:], b"money-profile-backup-v1")
    if destination.exists():
        raise FileExistsError("restore destination must not exist")
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_bytes(plaintext)
    destination.chmod(0o600)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("source", type=Path)
    parser.add_argument("destination", type=Path)
    arguments = parser.parse_args()
    restore(arguments.source, arguments.destination)
