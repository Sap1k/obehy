"""Raw realtime archive: content-addressed zstd payloads with a daily append-only index.

Layout (BASE_PLAN.md section 18.4)::

    <root>/<source>/<channel>/<YYYY-MM-DD>/index.jsonl     one line per poll, UTC date
    <root>/<source>/<channel>/<YYYY-MM-DD>/objects/<sha256>.zst
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

import zstandard

from obehy.pipeline.files import atomic_output_path

INDEX = "index.jsonl"
OBJECTS = "objects"
COMPRESSION_LEVEL = 10


def timestamp(value: datetime) -> str:
    return value.isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Poll:
    """One request to a channel, successful or not."""

    requested_at: datetime
    received_at: datetime
    status: int | None
    body: bytes | None
    content_type: str | None = None
    headers: Mapping[str, str] = field(default_factory=dict[str, str])
    error: str | None = None
    # What a demand-polled request was for (a board's station: {"sr70": "534149"}); archived
    # with the poll, since the payload itself may not say.
    request: Mapping[str, str] = field(default_factory=dict[str, str])

    @property
    def ok(self) -> bool:
        return self.error is None and self.status is not None and 200 <= self.status < 300


@dataclass(frozen=True)
class Stored:
    sha256: str | None
    new_object_bytes: int


def day_directory(root: Path, source: str, channel: str, day: date) -> Path:
    return root / source / channel / day.isoformat()


class ArchiveWriter:
    """Appends polls; each write opens the index anew, so days roll over without state."""

    def __init__(self, root: Path) -> None:
        self.root = root
        self._compressor = zstandard.ZstdCompressor(level=COMPRESSION_LEVEL)

    def append(
        self,
        source: str,
        channel: str,
        poll: Poll,
        body: bytes | None,
        extra: Mapping[str, object] | None = None,
    ) -> Stored:
        """Store `body` (the payload as archived) and one index line describing `poll`."""

        directory = day_directory(self.root, source, channel, poll.received_at.date())
        sha256: str | None = None
        written = 0
        if body is not None:
            sha256 = hashlib.sha256(body).hexdigest()
            blob = directory / OBJECTS / f"{sha256}.zst"
            if not blob.exists():
                compressed = self._compressor.compress(body)
                with atomic_output_path(blob) as temporary:
                    temporary.write_bytes(compressed)
                written = len(compressed)
        entry: dict[str, object] = {
            "requested_at": timestamp(poll.requested_at),
            "received_at": timestamp(poll.received_at),
            "elapsed_ms": round((poll.received_at - poll.requested_at) / timedelta(milliseconds=1)),
            "status": poll.status,
            "sha256": sha256,
            "bytes": None if body is None else len(body),
            "content_type": poll.content_type,
            "headers": dict(sorted(poll.headers.items())),
            "error": poll.error,
            **(extra or {}),
        }
        directory.mkdir(parents=True, exist_ok=True)
        line = json.dumps(entry, ensure_ascii=False, separators=(",", ":")) + "\n"
        with (directory / INDEX).open("a", encoding="utf-8", newline="\n") as index:
            index.write(line)
        return Stored(sha256, written)


@dataclass(frozen=True)
class ArchivedPoll:
    directory: Path
    entry: dict[str, Any]

    @property
    def received_at(self) -> datetime:
        return datetime.fromisoformat(cast(str, self.entry["received_at"]))

    def body(self) -> bytes | None:
        sha256 = cast(str | None, self.entry.get("sha256"))
        if sha256 is None:
            return None
        blob = self.directory / OBJECTS / f"{sha256}.zst"
        return zstandard.ZstdDecompressor().decompress(blob.read_bytes())


def iter_polls(
    root: Path, source: str, channel: str, start: date, end: date
) -> Iterator[ArchivedPoll]:
    """Archived polls of the UTC days `start`..`end` inclusive, in recording order.

    A truncated last index line (the recorder was killed mid-write) is skipped.
    """

    day = start
    while day <= end:
        directory = day_directory(root, source, channel, day)
        index = directory / INDEX
        if index.is_file():
            lines = index.read_text(encoding="utf-8").split("\n")
            for number, line in enumerate(lines):
                if not line:
                    continue
                try:
                    entry = cast(dict[str, Any], json.loads(line))
                except json.JSONDecodeError:
                    if number == len(lines) - 1:
                        break
                    raise
                yield ArchivedPoll(directory, entry)
        day += timedelta(days=1)
