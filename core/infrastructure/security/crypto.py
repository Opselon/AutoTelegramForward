"""AES-256-GCM encryption for data at rest (session strings, API keys)."""

import base64
import binascii
import hashlib
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM


class DecryptionError(ValueError):
    """A stored ciphertext cannot be read with the configured master key.

    Raised instead of leaking ``binascii.Error``/``InvalidTag``: a row written
    under a different ``ATF_MASTER_KEY`` (or truncated in transit) is a data
    problem with one row, and callers have to be able to name it without
    parsing a base64 traceback.
    """


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
        value = str(token or "").strip()
        if not value:
            return ""
        # urlsafe_b64decode rejects missing padding, and a token stored by an
        # older build (or hand-edited) is exactly what produced
        # "binascii.Error: Incorrect padding" from a *list* endpoint: one bad
        # row took down the whole page. Pad locally and report a typed error.
        padded = value + "=" * (-len(value) % 4)
        try:
            raw = base64.urlsafe_b64decode(padded.encode("ascii"))
        except (binascii.Error, ValueError, UnicodeEncodeError) as exc:
            raise DecryptionError("ciphertext is not valid base64") from exc
        if len(raw) < 13:
            raise DecryptionError("ciphertext is too short to hold a nonce and tag")
        nonce, ct = raw[:12], raw[12:]
        try:
            return self._aes.decrypt(nonce, ct, None).decode("utf-8")
        except (InvalidTag, ValueError) as exc:
            raise DecryptionError("ciphertext cannot be decrypted with this master key") from exc
