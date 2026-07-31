#!/usr/bin/env python3
"""Create an encrypted, compressed snapshot of AXE data and upload it to S3/R2."""

from __future__ import annotations

import argparse
import datetime
import gzip
import logging
import os
import shutil
import sys
import tarfile
import tempfile
from contextlib import suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

with suppress(ImportError):
    from cryptography.fernet import Fernet  # type: ignore[import-untyped]

with suppress(ImportError):
    import boto3  # type: ignore[import-untyped]
    from botocore.config import Config as BotoConfig  # type: ignore[import-untyped]

logger = logging.getLogger(__name__)


def _parse_sqlite_path(database_url: str) -> Path | None:
    """Extract the filesystem path from a SQLite URL."""
    parsed = urlparse(database_url)
    if parsed.scheme not in ("sqlite", "sqlite+aiosqlite"):
        return None
    path = parsed.path or parsed.netloc
    if not path:
        return None
    # sqlite:///./data/axe.db yields path '/./data/axe.db'
    if path.startswith("/"):
        path = path[1:]
    return Path(path).resolve()


def _encrypt(data: bytes, key: str) -> bytes:
    """Encrypt ``data`` with a Fernet key."""
    return Fernet(key.encode()).encrypt(data)


def _upload(
    local_path: Path,
    bucket: str,
    key: str,
    endpoint_url: str | None,
    region: str,
    access_key: str | None,
    secret_key: str | None,
) -> dict[str, Any]:
    """Upload ``local_path`` to an S3-compatible object store."""
    boto = globals().get("boto3")
    if boto is None:
        raise RuntimeError("boto3 is required for S3/R2 upload")

    client = boto.client(
        "s3",
        region_name=region,
        endpoint_url=endpoint_url,
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=BotoConfig(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=30,
        ),
    )
    response = client.upload_file(str(local_path), bucket, key)
    return {"bucket": bucket, "key": key, "response": response}


def create_backup(
    database_url: str,
    chroma_dir: Path,
    output_dir: Path,
    encryption_key: str | None = None,
    dry_run: bool = False,
    bucket: str | None = None,
    s3_endpoint: str | None = None,
    s3_region: str = "auto",
    s3_access_key: str | None = None,
    s3_secret_key: str | None = None,
) -> dict[str, Any]:
    """Snapshot SQLite DB and Chroma data, optionally encrypt and upload.

    Returns a result dictionary describing the archive path and upload status.
    """
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_name = f"axe-backup-{timestamp}.tar.gz"
    local_archive = output_dir / archive_name
    output_dir.mkdir(parents=True, exist_ok=True)

    db_path = _parse_sqlite_path(database_url)
    if db_path is None:
        raise ValueError(f"Unsupported database_url for backup: {database_url}")

    logger.info("Backing up DB=%s CHROMA=%s", db_path, chroma_dir)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        snapshot_dir = tmp_path / "axe-snapshot"
        snapshot_dir.mkdir()

        if db_path.exists():
            shutil.copy2(db_path, snapshot_dir / db_path.name)
        else:
            logger.warning("Database file not found: %s", db_path)

        chroma_dest = snapshot_dir / "chroma"
        if chroma_dir.exists():
            shutil.copytree(chroma_dir, chroma_dest)
        else:
            logger.warning("Chroma directory not found: %s", chroma_dir)
            chroma_dest.mkdir()

        snapshot_tar = tmp_path / "snapshot.tar.gz"
        with tarfile.open(snapshot_tar, "w:gz") as tar:
            tar.add(snapshot_dir, arcname="axe-snapshot")

        payload = snapshot_tar.read_bytes()
        if encryption_key:
            payload = _encrypt(payload, encryption_key)
            local_archive = local_archive.with_suffix(local_archive.suffix + ".enc")

        if dry_run:
            local_archive.write_bytes(gzip.compress(payload))
        else:
            local_archive.write_bytes(payload)

    result: dict[str, Any] = {
        "archive": str(local_archive),
        "database_path": str(db_path),
        "chroma_path": str(chroma_dir),
        "encrypted": encryption_key is not None,
        "dry_run": dry_run,
    }

    if not dry_run and bucket:
        s3_key = f"backups/{local_archive.name}"
        result["upload"] = _upload(
            local_archive,
            bucket,
            s3_key,
            s3_endpoint,
            s3_region,
            s3_access_key,
            s3_secret_key,
        )

    logger.info("Backup complete: %s", local_archive)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", "sqlite+aiosqlite:///./data/axe.db"),
    )
    parser.add_argument(
        "--chroma-dir",
        type=Path,
        default=Path(os.getenv("CHROMA_PERSIST_DIR", "./data/chroma")),
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.getenv("BACKUP_OUTPUT_DIR", "./data/backups")),
    )
    parser.add_argument("--encryption-key", default=os.getenv("BACKUP_ENCRYPTION_KEY"))
    parser.add_argument("--bucket", default=os.getenv("BACKUP_BUCKET"))
    parser.add_argument("--s3-endpoint", default=os.getenv("S3_ENDPOINT"))
    parser.add_argument("--s3-region", default=os.getenv("AWS_REGION", "auto"))
    parser.add_argument("--s3-access-key", default=os.getenv("AWS_ACCESS_KEY_ID"))
    parser.add_argument("--s3-secret-key", default=os.getenv("AWS_SECRET_ACCESS_KEY"))
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--log-level", default=os.getenv("LOG_LEVEL", "INFO"))
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    try:
        result = create_backup(
            database_url=args.database_url,
            chroma_dir=args.chroma_dir,
            output_dir=args.output_dir,
            encryption_key=args.encryption_key,
            dry_run=args.dry_run,
            bucket=args.bucket,
            s3_endpoint=args.s3_endpoint,
            s3_region=args.s3_region,
            s3_access_key=args.s3_access_key,
            s3_secret_key=args.s3_secret_key,
        )
        print(result["archive"])
        return 0
    except Exception as exc:  # pragma: no cover
        logger.exception("Backup failed: %s", exc)
        return 1


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
