"""Shared deterministic and atomic pipeline primitives."""

from __future__ import annotations

import hashlib
import json
import os
import uuid
from collections.abc import Generator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path


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


def write_json(path: Path, value: object) -> None:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    with atomic_output_path(path) as temporary:
        temporary.write_text(payload, encoding="utf-8", newline="\n")
