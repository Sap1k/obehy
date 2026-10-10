"""One HTTP request of a channel, as a `Poll` (successful or not).

A channel that declares an egress is never fetched directly: `fetch` refuses it, and only
`runtime.egress` sends it, through its proxy pool (docs/R2_SLICE.md section 7).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from urllib.error import HTTPError
from urllib.request import OpenerDirector, Request, urlopen

from obehy.pipeline.download import http_request
from obehy.realtime.archive import Poll
from obehy.realtime.manifest import Channel

KEPT_HEADERS = ("age", "date", "etag", "last-modified")

Clock = Callable[[], datetime]


class _Response(Protocol):
    status: int
    headers: Any

    def read(self) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


def utc_clock() -> datetime:
    return datetime.now(UTC)


def _kept_headers(headers: Any) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in KEPT_HEADERS:
        value = cast(str | None, headers.get(name))
        if value is not None:
            result[name] = value
    return result


def refused(channel: Channel, reason: str, clock: Clock = utc_clock) -> Poll:
    """A poll that was never sent."""

    now = clock()
    return Poll(requested_at=now, received_at=now, status=None, body=None, error=reason)


def send(
    request: Request,
    timeout_s: float,
    clock: Clock = utc_clock,
    opener: OpenerDirector | None = None,
) -> Poll:
    requested_at = clock()
    try:
        response = (
            opener.open(request, timeout=timeout_s)
            if opener is not None
            else urlopen(request, timeout=timeout_s)
        )
        with cast(_Response, response) as response:
            body = response.read()
            return Poll(
                requested_at=requested_at,
                received_at=clock(),
                status=response.status,
                body=body,
                content_type=cast(str | None, response.headers.get("Content-Type")),
                headers=_kept_headers(response.headers),
            )
    except HTTPError as error:
        try:
            body = error.read()
        except OSError:
            body = None
        return Poll(
            requested_at=requested_at,
            received_at=clock(),
            status=error.code,
            body=body or None,
            content_type=error.headers.get("Content-Type") if error.headers else None,
            headers=_kept_headers(error.headers) if error.headers else {},
            error=f"HTTP {error.code} {error.reason}",
        )
    except (OSError, ValueError) as error:
        return Poll(
            requested_at=requested_at,
            received_at=clock(),
            status=None,
            body=None,
            error=f"{type(error).__name__}: {error}",
        )


def channel_request(channel: Channel, *, anonymous: bool, body: bytes | None = None) -> Request:
    """The channel's request; an anonymous one carries only the manifest's own headers."""

    data = channel.body if body is None else body
    if anonymous:
        request = Request(channel.url, data=data, headers=dict(channel.headers))
    else:
        request = http_request(channel.url, data=data, headers=channel.headers)
    request.method = channel.method
    return request


def fetch(channel: Channel, clock: Clock = utc_clock) -> Poll:
    """A direct request; refused for a channel that must go through an egress."""

    if channel.egress is not None:
        return refused(channel, f"egress {channel.egress!r} required: not sent directly", clock)
    return send(channel_request(channel, anonymous=False), channel.timeout_s, clock)
