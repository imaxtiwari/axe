"""Fernet-based encryption helpers and SQLAlchemy EncryptedJSON type."""

from __future__ import annotations

import base64
import json
import os
from typing import Any, ClassVar

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy import Text, TypeDecorator

from axe.config import get_settings


def generate_fernet_key() -> str:
    """Generate a URL-safe base64-encoded Fernet key."""
    return Fernet.generate_key().decode("utf-8")


def _derive_key(raw_key: str | None) -> bytes:
    """Return a valid Fernet key from the provided raw key or env fallback."""
    if raw_key is None:
        raw_key = get_settings().encryption_key
    if not raw_key:
        # For local-only unsafe use (tests with no key set). Never use in prod.
        raw_key = os.environ.get("ENCRYPTION_KEY")
    if not raw_key:
        raise RuntimeError("ENCRYPTION_KEY is not configured")
    key = raw_key.encode("utf-8") if isinstance(raw_key, str) else raw_key
    if len(base64.urlsafe_b64decode(key + b"=" * (-len(key) % 4))) != 32:
        raise RuntimeError("ENCRYPTION_KEY is not a valid 32-byte Fernet key")
    return key


def get_fernet(key: str | None = None) -> Fernet:
    """Return a Fernet instance using the configured or provided key."""
    return Fernet(_derive_key(key))


def encrypt_plaintext(plaintext: str, key: str | None = None) -> str:
    """Encrypt plaintext and return URL-safe token string."""
    f = get_fernet(key)
    return f.encrypt(plaintext.encode("utf-8")).decode("utf-8")


def decrypt_ciphertext(ciphertext: str, key: str | None = None) -> str:
    """Decrypt ciphertext token and return plaintext string."""
    f = get_fernet(key)
    return f.decrypt(ciphertext.encode("utf-8")).decode("utf-8")


class EncryptionError(Exception):
    """Raised when encryption or decryption fails."""


class EncryptedJSON(TypeDecorator[dict[str, Any]]):
    """SQLAlchemy column type that transparently encrypts a JSON-serializable dict.

    On bind (write): serializes to JSON, encrypts with Fernet, stores token as text.
    On process result (read): decrypts token, deserializes JSON.
    """

    impl = Text
    cache_ok = True
    _key: ClassVar[str | None] = None

    @classmethod
    def configure(cls, key: str) -> None:
        """Configure the encryption key used by this column type."""
        cls._key = key

    def _get_fernet(self) -> Fernet:
        """Return a Fernet instance configured for this type."""
        if self._key is not None:
            return get_fernet(self._key)
        return get_fernet()

    def process_bind_param(self, value: Any | None, dialect: Any) -> str | None:
        if value is None:
            return None
        f = self._get_fernet()
        return f.encrypt(json.dumps(value).encode("utf-8")).decode("utf-8")

    def process_result_value(self, value: str | None, dialect: Any) -> Any | None:
        if value is None:
            return None
        f = self._get_fernet()
        try:
            decrypted = f.decrypt(value.encode("utf-8"))
        except InvalidToken as exc:
            raise EncryptionError("Failed to decrypt field — invalid key or ciphertext") from exc
        return json.loads(decrypted.decode("utf-8"))
