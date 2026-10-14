"""One HTTP request of a channel, as a `Poll` (successful or not).

A channel that declares an egress is never fetched directly: `fetch` refuses it, and only
`runtime.egress` sends it, through its proxy pool (docs/R2_SLICE.md section 7).

`{epoch_ms}` in a channel's URL is filled with the current time at each request (a cache
buster, DPMD-Q9). A gzip-encoded response, which a manifest asks for with its own
`accept-encoding` header, is stored decompressed. A channel with `auth` needs its secret, which
only `runtime.egress.Egress` holds: `fetch` refuses it.
"""

from __future__ import annotations

import gzip
import ssl
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Protocol, cast
from urllib.error import HTTPError
from urllib.parse import urlencode
from urllib.request import HTTPSHandler, OpenerDirector, Request, build_opener

from obehy.pipeline.download import http_request
from obehy.realtime.archive import Poll
from obehy.realtime.manifest import Channel

KEPT_HEADERS = ("age", "date", "etag", "last-modified")
EPOCH_MS = "{epoch_ms}"

Clock = Callable[[], datetime]

# One TLS context for every request: without it each HTTPS connection builds its own and loads
# the CA bundle again, which at a few polls a second is real CPU. Contexts are thread-safe.
TLS = ssl.create_default_context()
_DIRECT = build_opener(HTTPSHandler(context=TLS))


class _Response(Protocol):
    status: int
    headers: Any

    def read(self) -> bytes: ...

    def __enter__(self) -> _Response: ...

    def __exit__(self, *args: object) -> None: ...


def utc_clock() -> datetime:
    return datetime.now(UTC)


def _kept_headers(headers: Any, extra: tuple[str, ...] = ()) -> dict[str, str]:
    result: dict[str, str] = {}
    for name in (*KEPT_HEADERS, *extra):
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
    keep: tuple[str, ...] = (),
) -> Poll:
    """Send `request`; the poll keeps the response headers of `KEPT_HEADERS` and `keep`."""

    requested_at = clock()
    try:
        response = (opener or _DIRECT).open(request, timeout=timeout_s)
        with cast(_Response, response) as response:
            body = response.read()
            if response.headers.get("Content-Encoding") == "gzip":
                body = gzip.decompress(body)
            return Poll(
                requested_at=requested_at,
                received_at=clock(),
                status=response.status,
                body=body,
                content_type=cast(str | None, response.headers.get("Content-Type")),
                headers=_kept_headers(response.headers, keep),
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
    except (OSError, EOFError, ValueError) as error:  # gzip: BadGzipFile, truncated
        return Poll(
            requested_at=requested_at,
            received_at=clock(),
            status=None,
            body=None,
            error=f"{type(error).__name__}: {error}",
        )


def channel_request(
    channel: Channel,
    *,
    anonymous: bool,
    body: bytes | None = None,
    headers: Mapping[str, str] | None = None,
    params: Mapping[str, str] | None = None,
) -> Request:
    """The channel's request, with `headers` (a secret) and query `params` (a cursor) added; an
    anonymous one carries no header but the manifest's and these."""

    data = channel.body if body is None else body
    url = channel.url.replace(EPOCH_MS, str(time.time_ns() // 1_000_000))
    if params:
        url += ("&" if "?" in url else "?") + urlencode(params)
    sent = {**channel.headers, **(headers or {})}
    if anonymous:
        request = Request(url, data=data, headers=sent)
    else:
        request = http_request(url, data=data, headers=sent)
    request.method = channel.method
    return request


def fetch_body(channel: Channel, body: bytes, clock: Clock = utc_clock) -> Poll:
    """A direct request with a per-read body (demand channels); refused like `fetch`."""

    if channel.egress is not None:
        return refused(channel, f"egress {channel.egress!r} required: not sent directly", clock)
    if channel.auth is not None:
        return refused(channel, f"secret {channel.auth.secret!r} required", clock)
    request = channel_request(channel, anonymous=False, body=body)
    return send(request, channel.timeout_s, clock)


def fetch(channel: Channel, clock: Clock = utc_clock) -> Poll:
    """A direct request; refused for a channel that must go through an egress."""

    if channel.egress is not None:
        return refused(channel, f"egress {channel.egress!r} required: not sent directly", clock)
    if channel.auth is not None:
        return refused(channel, f"secret {channel.auth.secret!r} required", clock)
    return send(channel_request(channel, anonymous=False), channel.timeout_s, clock)
