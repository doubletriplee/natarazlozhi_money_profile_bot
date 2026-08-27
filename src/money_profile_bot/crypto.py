from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


def _decode_key(value: str) -> bytes:
    try:
        key = base64.urlsafe_b64decode(value.encode())
    except Exception as exc:  # pragma: no cover - defensive configuration guard
        raise ValueError("encryption key must be URL-safe base64") from exc
    if len(key) != 32:
        raise ValueError("encryption key must decode to exactly 32 bytes")
    return key


class CryptoBox:
    """Authenticated encryption plus stable, non-reversible lookup digests."""

    VERSION = b"v1"

    def __init__(self, encryption_key: str, lookup_key: str) -> None:
        self._aes = AESGCM(_decode_key(encryption_key))
        self._lookup_key = _decode_key(lookup_key)

    def encrypt(self, value: str, *, context: str) -> str:
        nonce = os.urandom(12)
        ciphertext = self._aes.encrypt(nonce, value.encode("utf-8"), context.encode())
        return base64.urlsafe_b64encode(self.VERSION + nonce + ciphertext).decode()

    def decrypt(self, token: str, *, context: str) -> str:
        raw = base64.urlsafe_b64decode(token.encode())
        if raw[:2] != self.VERSION:
            raise ValueError("unsupported ciphertext version")
        return self._aes.decrypt(raw[2:14], raw[14:], context.encode()).decode("utf-8")

    def encrypt_json(self, value: Any, *, context: str) -> str:
        payload = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
        return self.encrypt(payload, context=context)

    def decrypt_json(self, token: str, *, context: str) -> Any:
        return json.loads(self.decrypt(token, context=context))

    def lookup(self, value: str, *, context: str) -> str:
        message = f"{context}\0{value}".encode()
        return hmac.new(self._lookup_key, message, hashlib.sha256).hexdigest()
