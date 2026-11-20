"""Authenticated encryption for the Apple refresh token at rest.

An Apple refresh token is a long-lived credential: whoever holds it can mint new Apple access
tokens for that golfer. The database only ever stores ciphertext under a server-only key, and the
service never logs, returns or prints the plaintext.

Format: ``v1.<base64url nonce>.<base64url ciphertext>`` with AES-256-GCM and a fixed
additional-authenticated-data label, so a ciphertext cannot be moved to a different purpose.
"""

from __future__ import annotations

import base64
import binascii
import os

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

VERSION = "v1"
AAD = b"swingguru.apple.refresh_token.v1"
NONCE_BYTES = 12
KEY_BYTES = 32


class TokenBoxError(Exception):
    """The key or the stored token is unusable; the caller must fail closed."""


def _b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _unb64(text: str) -> bytes:
    padding = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + padding)


class TokenBox:
    """Seal/open a secret with an AES-256-GCM key supplied as base64."""

    def __init__(self, key: str) -> None:
        try:
            raw = base64.b64decode(key, validate=True)
        except (ValueError, binascii.Error):
            raise TokenBoxError("token key is not base64") from None
        if len(raw) != KEY_BYTES:
            raise TokenBoxError(f"token key must decode to {KEY_BYTES} bytes")
        self._aead = AESGCM(raw)

    def seal(self, plaintext: str) -> str:
        nonce = os.urandom(NONCE_BYTES)
        ciphertext = self._aead.encrypt(nonce, plaintext.encode("utf-8"), AAD)
        return f"{VERSION}.{_b64(nonce)}.{_b64(ciphertext)}"

    def open(self, token: str) -> str:
        parts = (token or "").split(".")
        if len(parts) != 3 or parts[0] != VERSION:
            raise TokenBoxError("unrecognised stored token format")
        try:
            nonce, ciphertext = _unb64(parts[1]), _unb64(parts[2])
            return self._aead.decrypt(nonce, ciphertext, AAD).decode("utf-8")
        except (InvalidTag, ValueError, binascii.Error, UnicodeDecodeError):
            raise TokenBoxError("stored token could not be decrypted") from None


__all__ = ["AAD", "KEY_BYTES", "NONCE_BYTES", "VERSION", "TokenBox", "TokenBoxError"]
