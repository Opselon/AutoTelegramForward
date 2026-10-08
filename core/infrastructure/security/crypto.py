"""AES-256-GCM encryption for data at rest (session strings, API keys)."""

import base64
import hashlib
import os

from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class CryptoService:
    def __init__(self, master_key: str) -> None:
        if not master_key:
            raise ValueError("master_key must not be empty")
        # Derive a stable 256-bit key from the configured secret.
        self._key = hashlib.sha256(master_key.encode("utf-8")).digest()
        self._aes = AESGCM(self._key)

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        ct = self._aes.encrypt(nonce, plaintext.encode("utf-8"), None)
        return base64.urlsafe_b64encode(nonce + ct).decode("ascii")

    def decrypt(self, token: str) -> str:
        raw = base64.urlsafe_b64decode(token.encode("ascii"))
        nonce, ct = raw[:12], raw[12:]
        return self._aes.decrypt(nonce, ct, None).decode("utf-8")
