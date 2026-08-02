from __future__ import annotations

import re
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from obehy.persistence.models import SourceSnapshotArtifactRow, SourceSnapshotRow

SHA256 = re.compile(r"^[0-9a-f]{64}$")


def _relative_storage_key(value: str) -> str:
    path = PurePosixPath(value)
    if path.is_absolute() or ":" in path.parts[0] or ".." in path.parts:
        raise ValueError("Artifact keys must be normalized storage-relative paths")
    return path.as_posix()


class SourceSnapshotService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create_or_get(
        self,
        *,
        source_id: str,
        content_sha256: str,
        retrieved_at: datetime,
        artifact_key: str,
        declared_version: str | None = None,
        manifest: dict[str, Any] | None = None,
    ) -> int:
        if not SHA256.fullmatch(content_sha256):
            raise ValueError("Snapshot SHA-256 must be lowercase hexadecimal")
        artifact_key = _relative_storage_key(artifact_key)
        existing = self.session.scalar(
            select(SourceSnapshotRow).where(
                SourceSnapshotRow.source_id == source_id,
                SourceSnapshotRow.content_sha256 == content_sha256,
            )
        )
        if existing is not None:
            return existing.id
        row = SourceSnapshotRow(
            source_id=source_id,
            content_sha256=content_sha256,
            retrieved_at=retrieved_at,
            declared_version=declared_version,
            artifact_key=artifact_key,
            manifest=manifest or {},
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def add_artifact(
        self,
        snapshot_id: int,
        *,
        logical_role: str,
        storage_key: str,
        content_sha256: str,
        size_bytes: int,
        media_type: str | None = None,
    ) -> None:
        if not SHA256.fullmatch(content_sha256):
            raise ValueError("Artifact SHA-256 must be lowercase hexadecimal")
        if size_bytes < 0:
            raise ValueError("Artifact size cannot be negative")
        self.session.add(
            SourceSnapshotArtifactRow(
                snapshot_id=snapshot_id,
                logical_role=logical_role,
                storage_key=_relative_storage_key(storage_key),
                content_sha256=content_sha256,
                size_bytes=size_bytes,
                media_type=media_type,
            )
        )
        self.session.flush()
