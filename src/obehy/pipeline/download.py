"""HTTP downloads with retries and retrieval records."""

from __future__ import annotations

import hashlib
import http.client
import os
import ssl
import threading
import time
from collections.abc import Callable, Generator, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol, cast
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from obehy.pipeline.errors import PipelineError
from obehy.pipeline.files import utc_now
from obehy.pipeline.reporting import Reporter

USER_AGENT = "Obehy/0.1 (+https://obehy.cz)"
# Pauses before the second and third attempt; a source fails after len + 1 attempts.
RETRY_DELAYS: tuple[float, ...] = (5.0, 15.0)

_sleep = time.sleep


class FetchLog:
    """Thread-safe record of fetch attempts, written next to a run's sources."""

    def __init__(self) -> None:
        self.attempts: list[dict[str, object]] = []
        self._lock = threading.Lock()

    def add(self, entry: dict[str, object]) -> None:
        with self._lock:
            self.attempts.append(entry)


_active_log: FetchLog | None = None


@contextmanager
def fetch_log() -> Generator[FetchLog]:
    """Record every fetch attempt made while the context is open."""

    global _active_log
    previous, _active_log = _active_log, FetchLog()
    try:
        yield _active_log
    finally:
        _active_log = previous


def record_attempt(
    name: str,
    url: str,
    attempt: int,
    started_at: str,
    seconds: float,
    outcome: str,
    error: BaseException | None = None,
) -> None:
    if _active_log is None:
        return
    entry: dict[str, object] = {
        "name": name,
        "url": url,
        "attempt": attempt,
        "started_at": started_at,
        "seconds": round(seconds, 3),
        "outcome": outcome,
    }
    if error is not None:
        entry["error_type"] = type(error).__name__
        entry["error"] = reason(error)
    _active_log.add(entry)


class DownloadError(PipelineError):
    """A source could not be fetched; the message names the source and its URL."""

    def __init__(self, name: str, url: str, attempts: int, reason: str) -> None:
        plural = "" if attempts == 1 else "s"
        super().__init__(
            f"Download {name} failed after {attempts} attempt{plural}: {url}: {reason}"
        )
        self.name = name
        self.url = url
        self.attempts = attempts


def http_request(
    url: str, *, data: bytes | None = None, headers: Mapping[str, str] | None = None
) -> Request:
    return Request(url, data=data, headers={"User-Agent": USER_AGENT, **(headers or {})})


def reason(error: BaseException) -> str:
    if isinstance(error, HTTPError):
        return f"HTTP {error.code} {error.reason}"
    if isinstance(error, URLError):
        return str(error.reason)
    return str(error) or type(error).__name__


def _network_error(error: BaseException) -> bool:
    return isinstance(error, (URLError, OSError, http.client.HTTPException))


def _retryable(error: BaseException) -> bool:
    if isinstance(error, HTTPError):
        return error.code >= 500 or error.code == 429
    return isinstance(
        error,
        (URLError, TimeoutError, ConnectionError, ssl.SSLError, http.client.HTTPException),
    )


def sleep_before_retry(attempt: int) -> None:
    """Wait before attempt ``attempt + 1`` of a fetch that failed ``attempt`` times."""

    _sleep(RETRY_DELAYS[attempt - 1])


def with_retries[T](
    name: str,
    url: str,
    fetch: Callable[[], T],
    *,
    reporter: Reporter | None = None,
) -> T:
    """Run one fetch with retries on transient network errors.

    Network errors surface as `DownloadError`; anything else (validation, programming
    errors) propagates unchanged after a single attempt. Attempts go to the active fetch log.
    """

    for attempt in range(1, len(RETRY_DELAYS) + 2):
        started_at = utc_now()
        started = time.monotonic()
        try:
            result = fetch()
        except Exception as error:
            seconds = time.monotonic() - started
            retry = _retryable(error) and attempt <= len(RETRY_DELAYS)
            record_attempt(
                name, url, attempt, started_at, seconds, "retry" if retry else "failed", error
            )
            if retry:
                if reporter is not None:
                    reporter.note(
                        f"Download {name} attempt {attempt} failed after {seconds:.1f}s "
                        f"({reason(error)}); retrying in {RETRY_DELAYS[attempt - 1]:.0f}s"
                    )
                sleep_before_retry(attempt)
                continue
            if _network_error(error):
                raise DownloadError(name, url, attempt, reason(error)) from error
            raise
        record_attempt(name, url, attempt, started_at, time.monotonic() - started, "ok")
        return result
    raise AssertionError("unreachable download retry state")


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
    url: str,
    *,
    data: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    name: str | None = None,
    reporter: Reporter | None = None,
) -> bytes:
    """Fetch a small HTTP response body."""

    def fetch() -> bytes:
        with cast(
            _Response, urlopen(http_request(url, data=data, headers=headers), timeout=120)
        ) as response:
            return response.read()

    return with_retries(name or url, url, fetch, reporter=reporter)


DownloadFn = Callable[[str, Path, str, Reporter | None], DownloadRecord]


def download_file(
    url: str,
    destination: Path,
    name: str,
    reporter: Reporter | None = None,
) -> DownloadRecord:
    """Stream ``url`` to ``destination``, retrying transient failures from the start.

    A failed attempt deliberately keeps its .part file: failed builds retain their entire
    staging directory for diagnosis.
    """

    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")

    def fetch() -> DownloadRecord:
        retrieved_at = utc_now()
        task: int | None = None
        sha256 = hashlib.sha256()
        md5 = hashlib.md5()
        downloaded = 0
        try:
            response_context = cast(_Response, urlopen(http_request(url), timeout=120))
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
        except Exception as error:
            if reporter is not None and task is not None:
                reporter.finish(task, f"failed after {downloaded:,} bytes: {reason(error)}")
            raise
        os.replace(temporary, destination)
        if reporter is not None and task is not None:
            reporter.finish(task, f"{downloaded:,} bytes")
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

    return with_retries(name, url, fetch, reporter=reporter)
