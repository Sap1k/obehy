"""HTTP downloads with retrieval records."""

from __future__ import annotations

import hashlib
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib.request import Request, urlopen

from obehy.pipeline.files import utc_now
from obehy.pipeline.reporting import Reporter

USER_AGENT = "Obehy/0.1 (+https://obehy.cz)"


def http_request(
    url: str, *, data: bytes | None = None, headers: Mapping[str, str] | None = None
) -> Request:
    return Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})


class _Response(Protocol):
    headers: Mapping[str, str]

    def read(self, size: int = -1) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


@dataclass(frozen=True)
class DownloadRecord:
    name: str
    url: str
    retrieved_at: str
    bytes: int
    sha256: str
    etag: str | None
    last_modified: str | None
    md5: str | None = None


def read_url(
    url: str, *, data: bytes | None = None, headers: Mapping[str, str] | None = None
) -> bytes:
    """Fetch a small HTTP response body."""

    with cast(
        _Response, urlopen(http_request(url, data=data, headers=headers), timeout=120)
    ) as response:
        return response.read()


DownloadFn = Callable[[str, Path, str, Reporter | None], DownloadRecord]


def download_file(
    url: str,
    destination: Path,
    name: str,
    reporter: Reporter | None = None,
) -> DownloadRecord:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    retrieved_at = utc_now()
    task: int | None = None
    sha256 = hashlib.sha256()
    md5 = hashlib.md5()
    downloaded = 0
    try:
        request = http_request(url)
        response_context = cast(_Response, urlopen(request, timeout=120))
        with response_context as response, temporary.open("wb") as output:
            length_text = response.headers.get("Content-Length")
            total = int(length_text) if length_text and length_text.isdigit() else None
            if reporter is not None:
                task = reporter.start(f"Download {name}", total=total, unit="bytes")
            while chunk := response.read(1024 * 1024):
                output.write(chunk)
                sha256.update(chunk)
                md5.update(chunk)
                downloaded += len(chunk)
                if reporter is not None and task is not None:
                    reporter.update(task, completed=downloaded)
            headers = response.headers
        os.replace(temporary, destination)
        if reporter is not None and task is not None:
            reporter.finish(task, f"{downloaded:,} bytes")
    except Exception:
        # Deliberately keep the .part file: failed builds retain their entire
        # staging directory for diagnosis and possible resumability work.
        raise

    return DownloadRecord(
        name=name,
        url=url,
        retrieved_at=retrieved_at,
        bytes=downloaded,
        sha256=sha256.hexdigest(),
        etag=headers.get("ETag"),
        last_modified=headers.get("Last-Modified"),
        md5=md5.hexdigest(),
    )
