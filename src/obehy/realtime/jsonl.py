"""JSON-lines files of `obehy-rt compare` and the replay diagnostics: text, gzip when the name
ends in `.gz` (deterministic: no name, mtime 0, so equal streams give equal files)."""

from __future__ import annotations

import gzip
import io
from collections.abc import Generator
from contextlib import contextmanager
from pathlib import Path
from typing import IO, Literal


@contextmanager
def open_lines(path: Path, mode: Literal["r", "w"]) -> Generator[IO[str]]:
    """A JSON-lines file as text, gzip when its name ends in `.gz`."""

    if path.suffix != ".gz":
        with path.open(mode, encoding="utf-8", newline="\n") as stream:
            yield stream
        return
    with path.open(mode + "b") as raw:
        packed = gzip.GzipFile(filename="", mode=mode + "b", fileobj=raw, mtime=0)
        with packed, io.TextIOWrapper(packed, encoding="utf-8", newline="\n") as stream:
            yield stream
