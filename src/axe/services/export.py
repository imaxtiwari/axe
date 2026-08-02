"""Encrypted compliance export service.

Bundles an entity with its audit trail into a single JSON payload, encrypts it
with a configured Fernet key, and returns the ciphertext together with a
SHA-256 checksum of the plaintext archive.  The ciphertext is base64-encoded
for safe JSON transport.
"""

from __future__ import annotations

import base64
import hashlib
import json
import logging
from typing import Any, cast

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import DeclarativeBase

from axe.config import Settings, get_settings
from axe.db.models import AuditLog
from axe.security.encryption import EncryptionError, encrypt_plaintext, get_fernet

logger = logging.getLogger(__name__)


class ExportService:
    """Export an entity and its audit log as an encrypted, checksummed archive."""

    def __init__(
        self,
        session: AsyncSession,
        settings: Settings | None = None,
    ) -> None:
        self.session = session
        self.settings = settings or get_settings()

    def _export_key(self) -> str:
        """Return the Fernet key used to encrypt exports.

        Falls back to the general encryption key when no export-specific key
        is configured, so single-key deployments continue to work.
        """
        key = self.settings.export_encryption_key or self.settings.encryption_key
        if not key:
            raise RuntimeError("EXPORT_ENCRYPTION_KEY or ENCRYPTION_KEY is not configured")
        return key

    def _model_to_dict(self, instance: DeclarativeBase) -> dict[str, Any]:
        """Serialize a SQLAlchemy model instance to a JSON-safe dict."""
        result: dict[str, Any] = {}
        for column in instance.__table__.columns:
            value = getattr(instance, column.name, None)
            if isinstance(value, bytes):
                value = base64.b64encode(value).decode("utf-8")
            elif value is not None and hasattr(value, "isoformat"):
                value = value.isoformat()
            result[column.name] = value
        return result

    async def export(
        self,
        entity: DeclarativeBase,
        *,
        extra_metadata: dict[str, Any] | None = None,
    ) -> dict[str, str]:
        """Create an encrypted export archive for ``entity``.

        Args:
            entity: An ORM-mapped instance to export.
            extra_metadata: Optional metadata included in the archive header.

        Returns:
            A dict with ``object_type``, ``object_id``, ``encrypted_payload``,
            and ``sha256_checksum`` of the canonical plaintext archive.
        """
        object_type = entity.__tablename__
        object_id = getattr(entity, "id", None)

        where_filters = [AuditLog.object_type == object_type]
        if object_id is not None:
            where_filters.append(AuditLog.object_id == str(object_id))

        audit_result = await self.session.execute(
            select(AuditLog).where(*where_filters).order_by(AuditLog.created_at.asc())
        )
        audit_rows = audit_result.scalars().all()

        archive: dict[str, Any] = {
            "version": "axe-export-v1",
            "object_type": object_type,
            "object_id": str(object_id) if object_id is not None else None,
            "metadata": extra_metadata or {},
            "entity": self._model_to_dict(entity),
            "audit_trail": [self._model_to_dict(row) for row in audit_rows],
        }

        plaintext = json.dumps(archive, sort_keys=True, default=str).encode("utf-8")
        checksum = hashlib.sha256(plaintext).hexdigest()

        key = self._export_key()
        # encrypt_plaintext returns a URL-safe Fernet token; encode to base64
        # so the output is unambiguously a string field in JSON.
        token = encrypt_plaintext(plaintext.decode("utf-8"), key)
        ciphertext = base64.b64encode(token.encode("utf-8")).decode("utf-8")

        return {
            "object_type": object_type,
            "object_id": str(object_id) if object_id is not None else "",
            "encrypted_payload": ciphertext,
            "sha256_checksum": checksum,
        }

    @classmethod
    def decrypt(
        cls,
        encrypted_payload: str,
        key: str,
    ) -> dict[str, Any]:
        """Decrypt an export payload and return the archive dict.

        Args:
            encrypted_payload: The ``encrypted_payload`` string from
                :meth:`export`, which is base64-wrapped Fernet ciphertext.
            key: The Fernet key used during encryption.

        Returns:
            The decrypted JSON archive.

        Raises:
            EncryptionError: If decryption or decoding fails.
        """
        try:
            token = base64.b64decode(encrypted_payload).decode("utf-8")
        except Exception as exc:
            raise EncryptionError("Export payload is not valid base64") from exc

        f = get_fernet(key)
        try:
            plaintext = f.decrypt(token.encode("utf-8")).decode("utf-8")
        except Exception as exc:
            raise EncryptionError("Failed to decrypt export payload") from exc

        return cast(dict[str, Any], json.loads(plaintext))
