"""Deterministic and atomic file primitives."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import uuid
import zipfile
from collections.abc import Generator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Literal

from obehy.pipeline.errors import PipelineError

if TYPE_CHECKING:
    from obehy.pipeline.reporting import Reporter


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


def file_digest(path: Path, algorithm: str = "sha256") -> str:
    digest = hashlib.new(algorithm)
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


@contextmanager
def atomic_output_path(destination: Path, suffix: str = ".part") -> Generator[Path]:
    """Yield a unique sibling path and atomically activate it on success."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(f".{destination.name}.{uuid.uuid4().hex}{suffix}")
    try:
        yield temporary
        os.replace(temporary, destination)
    finally:
        if temporary.exists():
            temporary.unlink()


def link_or_copy(source: Path, destination: Path) -> None:
    """Hard-link ``source`` to ``destination``, copying when the filesystem refuses links."""

    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.link(source, destination)
    except OSError:
        shutil.copy2(source, destination)


def write_json(path: Path, value: object) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with atomic_output_path(path) as temporary:
        temporary.write_text(payload, encoding="utf-8", newline="\n")


ZipCompression = Literal["fast", "balanced", "small"]


ZIP_COMPRESSION_LEVELS: dict[ZipCompression, int] = {
    "fast": 1,
    "balanced": 6,
    "small": 9,
}


@dataclass(frozen=True)
class ArtifactIdentity:
    bytes: int
    sha256: str


def deterministic_zip(
    source_directory: Path,
    destination: Path,
    reporter: Reporter | None = None,
    compression_level: int = 6,
) -> ArtifactIdentity:
    if not 0 <= compression_level <= 9:
        raise ValueError("ZIP compression level must be between 0 and 9")
    files = sorted(
        (path for path in source_directory.rglob("*") if path.is_file()),
        key=lambda path: path.relative_to(source_directory).as_posix(),
    )
    if not files:
        raise PipelineError(f"Cannot package empty directory: {source_directory}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    total_bytes = sum(path.stat().st_size for path in files)
    task = (
        reporter.start("Package merged JDF", total=total_bytes, unit="bytes") if reporter else None
    )
    with zipfile.ZipFile(
        destination,
        "w",
        compression=zipfile.ZIP_DEFLATED,
        compresslevel=compression_level,
    ) as archive:
        for path in files:
            relative = path.relative_to(source_directory).as_posix()
            size = path.stat().st_size
            info = zipfile.ZipInfo(relative, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.compress_level = compression_level
            info.external_attr = 0o100644 << 16
            info.file_size = size
            with (
                path.open("rb") as source,
                archive.open(
                    info,
                    "w",
                    force_zip64=size >= zipfile.ZIP64_LIMIT,
                ) as output,
            ):
                while chunk := source.read(1024 * 1024):
                    output.write(chunk)
                    if reporter is not None and task is not None:
                        reporter.update(task, advance=len(chunk), detail=relative)
    if reporter is not None and task is not None:
        reporter.finish(task, f"{len(files)} files, {destination.stat().st_size:,} bytes")
    return ArtifactIdentity(bytes=destination.stat().st_size, sha256=file_digest(destination))
