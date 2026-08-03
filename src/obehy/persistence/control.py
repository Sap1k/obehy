from __future__ import annotations

import hashlib
from datetime import datetime
from pathlib import PurePosixPath
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from obehy.persistence.models import (
    ArtifactRow,
    BuildSpecRow,
    SourceConfigRevisionRow,
    SourceRow,
    SourceSnapshotArtifactRow,
    SourceSnapshotRow,
)
from obehy.serving import canonical_json_bytes


class ControlDataError(ValueError):
    pass


def relative_storage_key(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or ".." in path.parts or ":" in path.parts[0]:
        raise ControlDataError("Artifact storage keys must be normalized relative paths")
    normalized = path.as_posix()
    if normalized != value:
        raise ControlDataError("Artifact storage keys must use canonical POSIX separators")
    return normalized


def document_sha256(document: object) -> str:
    return hashlib.sha256(canonical_json_bytes(document)).hexdigest()


class ControlRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def register_source(
        self,
        source_id: str,
        *,
        display_name: str,
        adapter_kind: str,
        licence: str | None = None,
        retrieval_method: str | None = None,
    ) -> None:
        row = self.session.get(SourceRow, source_id)
        values = (display_name, adapter_kind, licence, retrieval_method)
        if row is not None:
            existing = (row.display_name, row.adapter_kind, row.licence, row.retrieval_method)
            if existing != values:
                raise ControlDataError(f"Source {source_id!r} is already registered differently")
            return
        self.session.add(
            SourceRow(
                id=source_id,
                display_name=display_name,
                adapter_kind=adapter_kind,
                licence=licence,
                retrieval_method=retrieval_method,
            )
        )
        self.session.flush()

    def register_artifact(
        self,
        *,
        sha256: str,
        storage_key: str,
        size_bytes: int,
        media_type: str | None = None,
    ) -> None:
        key = relative_storage_key(storage_key)
        row = self.session.get(ArtifactRow, sha256)
        values = (key, size_bytes, media_type)
        if row is not None:
            if (row.storage_key, row.size_bytes, row.media_type) != values:
                raise ControlDataError("Artifact digest is already registered with different facts")
            return
        self.session.add(
            ArtifactRow(
                sha256=sha256,
                storage_key=key,
                size_bytes=size_bytes,
                media_type=media_type,
            )
        )
        self.session.flush()

    def add_source_config(
        self, source_id: str, document: dict[str, Any], *, schema_version: int = 1
    ) -> int:
        digest = document_sha256(document)
        existing = self.session.scalar(
            select(SourceConfigRevisionRow.id).where(
                SourceConfigRevisionRow.source_id == source_id,
                SourceConfigRevisionRow.sha256 == digest,
            )
        )
        if existing is not None:
            return existing
        row = SourceConfigRevisionRow(
            source_id=source_id,
            schema_version=schema_version,
            sha256=digest,
            config=document,
        )
        self.session.add(row)
        self.session.flush()
        return row.id

    def add_snapshot(
        self,
        source_id: str,
        *,
        payload_sha256: str,
        retrieved_at: datetime,
        manifest: dict[str, Any],
        declared_version: str | None = None,
        artifacts: dict[str, str] | None = None,
    ) -> int:
        manifest_sha256 = document_sha256(manifest)
        existing = self.session.scalar(
            select(SourceSnapshotRow.id).where(
                SourceSnapshotRow.source_id == source_id,
                SourceSnapshotRow.payload_sha256 == payload_sha256,
            )
        )
        if existing is not None:
            return existing
        row = SourceSnapshotRow(
            source_id=source_id,
            payload_sha256=payload_sha256,
            manifest_sha256=manifest_sha256,
            retrieved_at=retrieved_at,
            declared_version=declared_version,
            manifest=manifest,
        )
        self.session.add(row)
        self.session.flush()
        for role, artifact_sha256 in sorted((artifacts or {}).items()):
            self.session.add(
                SourceSnapshotArtifactRow(
                    snapshot_id=row.id,
                    role=role,
                    artifact_sha256=artifact_sha256,
                )
            )
        self.session.flush()
        return row.id

    def store_build_spec(self, document: dict[str, Any], *, schema_version: int = 1) -> str:
        digest = document_sha256(document)
        if self.session.get(BuildSpecRow, digest) is None:
            self.session.add(
                BuildSpecRow(
                    sha256=digest,
                    schema_version=schema_version,
                    document=document,
                )
            )
            self.session.flush()
        return digest
