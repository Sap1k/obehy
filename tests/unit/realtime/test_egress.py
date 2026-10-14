"""Proxied, anonymous egress (docs/R2_SLICE.md section 7) against local stub proxies."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from obehy.realtime.archive import Poll
from obehy.realtime.manifest import Auth, Channel, Cursor
from obehy.realtime.policy import EgressPolicy
from obehy.realtime.runtime.egress import (
    Egress,
    ProxyPool,
    load_egress_lists,
    parse_proxy_list,
)
from obehy.realtime.runtime.fetch import fetch
from obehy.realtime.runtime.scheduler import backoff_interval, failures_after
from obehy.runtime_config import load_secrets

POLICY = EgressPolicy(refresh_s=3600, cooldown_s=600)
T0 = datetime(2026, 10, 10, 12, 0, tzinfo=UTC)


def _channel(egress: str | None = "sz", url: str = "http://sz.invalid/trains") -> Channel:
    return Channel(
        source="sz-mapa",
        channel="trains",
        method="GET",
        url=url,
        interval_s=30.0,
        timeout_s=5.0,
        headers={"Accept": "application/json"},
        egress=egress,
        backoff_after=5,
        max_backoff_s=300.0,
    )


class _Proxy:
    """A forward HTTP proxy that answers every request itself with a fixed status."""

    def __init__(self, status: int) -> None:
        self.status = status
        self.seen: list[dict[str, str]] = []
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                proxy.seen.append({"path": self.path, **dict(self.headers.items())})
                body = b'{"ok": true}'
                self.send_response(proxy.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def line(self) -> str:
        return f"127.0.0.1:{self.server.server_address[1]}:user:p@ss"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture
def proxies() -> Iterator[tuple[_Proxy, _Proxy]]:
    refusing, working = _Proxy(403), _Proxy(200)
    yield refusing, working
    refusing.close()
    working.close()


def _pool(text: str, logs: list[str]) -> ProxyPool:
    pool = ProxyPool("sz", "secret.txt", POLICY, read=lambda _: text)
    pool.log = logs.append
    pool.refresh()
    return pool


def test_proxy_list_lines_become_proxy_urls() -> None:
    text = "10.0.0.1:8080:alice:s3cr:et\n10.0.0.2:8080:bob:p@ss\n\nnot a proxy\n10.0.0.3:x:a:b\n"
    assert parse_proxy_list(text) == ("http://bob:p%40ss@10.0.0.2:8080",)


def test_the_list_file_comes_from_the_environment_before_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "obehy.local.toml"
    config.write_text(
        'schema_version = 1\n[realtime.egress.sz]\nproxy_list_file = "D:/secrets/a.txt"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("OBEHY_EGRESS_SZ_PROXY_LIST_FILE", raising=False)
    assert load_egress_lists(["sz", "other"], config) == {"sz": "D:/secrets/a.txt"}
    monkeypatch.setenv("OBEHY_EGRESS_SZ_PROXY_LIST_FILE", "/app/secrets/b.txt")
    assert load_egress_lists(["sz"], config) == {"sz": "/app/secrets/b.txt"}


def test_an_egress_channel_is_never_fetched_directly() -> None:
    poll = fetch(_channel())
    assert poll.status is None and poll.error is not None and "egress" in poll.error


def test_an_egress_channel_without_a_pool_does_not_poll() -> None:
    logs: list[str] = []
    kept = Egress({}).usable([_channel(), _channel(egress=None)], logs.append)
    assert [c.egress for c in kept] == [None]
    assert "not polled" in logs[0]


def test_requests_go_through_the_proxy_without_a_user_agent(
    proxies: tuple[_Proxy, _Proxy],
) -> None:
    _, working = proxies
    logs: list[str] = []
    poll = Egress({"sz": _pool(working.line, logs)}).fetch(_channel())
    assert poll.ok and poll.body == b'{"ok": true}'
    (request,) = working.seen
    assert request["path"] == "http://sz.invalid/trains"  # a proxied (absolute-URI) request
    assert "User-Agent" not in request
    assert request["Proxy-Authorization"].startswith("Basic ")
    assert request["Accept"] == "application/json"
    assert "p@ss" not in " ".join(logs) and "127.0.0.1" not in " ".join(logs)


def test_a_refusing_proxy_is_benched_and_the_request_retried_on_the_next(
    proxies: tuple[_Proxy, _Proxy],
) -> None:
    refusing, working = proxies
    logs: list[str] = []
    pool = _pool(f"{refusing.line}\n{working.line}\n", logs)
    poll = Egress({"sz": pool}).fetch(_channel())
    assert poll.ok
    assert len(refusing.seen) == 1 and len(working.seen) == 1
    assert list(pool.benched_until) == [0]
    assert any("proxy #0 benched (HTTP 403)" in line for line in logs)
    Egress({"sz": pool}).fetch(_channel())
    assert len(refusing.seen) == 1  # still on the bench


def test_every_proxy_refusing_returns_the_refusal(proxies: tuple[_Proxy, _Proxy]) -> None:
    refusing, _ = proxies
    pool = _pool(refusing.line, [])
    assert Egress({"sz": pool}).fetch(_channel()).status == 403
    poll = Egress({"sz": pool}).fetch(_channel())
    assert poll.status is None and "every proxy benched" in str(poll.error)


def test_a_failed_list_download_keeps_the_pool() -> None:
    texts = iter(["10.0.0.1:1:a:b", ""])
    clock = iter([0.0, 4000.0])
    pool = ProxyPool(
        "sz",
        "secret.txt",
        POLICY,
        read=lambda _: next(texts),
        monotonic=lambda: next(clock),
        log=lambda _: None,
    )
    pool.refresh()
    pool.refresh()
    assert pool.proxies == ("http://a:b@10.0.0.1:1",)


def test_a_403_opens_the_circuit_at_once() -> None:
    channel = _channel()
    refused = Poll(T0, T0, 403, None, error="HTTP 403 Forbidden")
    failed = Poll(T0, T0, 503, None, error="HTTP 503")
    assert backoff_interval(channel, failures_after(channel, 0, failed)) == 30.0
    assert backoff_interval(channel, failures_after(channel, 0, refused)) == 300.0
    assert failures_after(channel, 40, Poll(T0, T0, 200, b"{}")) == 0


def test_the_saved_list_is_read_from_its_file(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = tmp_path / "proxies.txt"
    saved.write_text("10.0.0.1:8080:alice:secret\n", encoding="utf-8")
    config = tmp_path / "obehy.local.toml"
    config.write_text(
        f'schema_version = 1\n[realtime.egress.sz]\nproxy_list_file = "{saved.as_posix()}"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("OBEHY_EGRESS_SZ_PROXY_LIST_FILE", raising=False)
    (source,) = load_egress_lists(["sz"], config).values()
    pool = ProxyPool("sz", source, POLICY, log=lambda _: None)
    pool.refresh()
    assert pool.proxies == ("http://alice:secret@10.0.0.1:8080",)


class _Golemio:
    """A source that wants an API key and answers with a delta cursor header."""

    def __init__(self) -> None:
        self.seen: list[tuple[str, str | None]] = []
        self.status = 200
        source = self

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                source.seen.append((self.path, self.headers.get("X-Access-Token")))
                self.send_response(source.status)
                self.send_header("x-last-modified", f"2026-10-14T22:00:0{len(source.seen)}.000Z")
                self.send_header("Content-Length", "2")
                self.end_headers()
                self.wfile.write(b"{}")

            def log_message(self, format: str, *args: object) -> None:
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server.server_address[1]}/v2/vehiclepositions?limit=5"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


def _keyed(url: str) -> Channel:
    return Channel(
        source="pid",
        channel="vehicles",
        method="GET",
        url=url,
        interval_s=10.0,
        timeout_s=5.0,
        auth=Auth("X-Access-Token", "golemio"),
        cursor=Cursor("x-last-modified", "updatedSince", 120.0),
    )


def test_a_cursor_channel_polls_deltas_with_its_secret() -> None:
    source = _Golemio()
    now = [T0]
    egress = Egress({}, clock=lambda: now[0], secrets={"golemio": "k3y"})
    channel = _keyed(source.url)
    try:
        first = egress.fetch(channel)
        second = egress.fetch(channel)
        source.status = 503
        failed = egress.fetch(channel)
        source.status = 200
        after_gap = egress.fetch(channel)  # a failed poll keeps the cursor
        now[0] = T0 + timedelta(seconds=121)
        stale = egress.fetch(channel)  # the last good poll is too old: a full list
    finally:
        source.close()
    paths = [path for path, _ in source.seen]
    assert all(key == "k3y" for _, key in source.seen)
    assert paths[0] == "/v2/vehiclepositions?limit=5" and first.request == {}
    assert paths[1].endswith("&updatedSince=2026-10-14T22%3A00%3A01.000Z")
    assert second.request == {"updatedSince": "2026-10-14T22:00:01.000Z"}
    assert not failed.ok and paths[2] == paths[3]
    assert after_gap.ok and paths[3].endswith("updatedSince=2026-10-14T22%3A00%3A02.000Z")
    assert "updatedSince" not in paths[4] and stale.request == {}


def test_an_auth_channel_needs_its_secret() -> None:
    channel = _keyed("http://pid.invalid/")
    logs: list[str] = []
    assert Egress({}).usable([channel], logs.append) == []
    assert "OBEHY_SECRET_GOLEMIO" in logs[0] and "not polled" in logs[0]
    assert Egress({}, secrets={"golemio": "k"}).usable([channel], logs.append) == [channel]
    poll = fetch(channel)  # without the egress fetcher there is no secret: never sent
    assert poll.status is None and poll.error is not None and "secret" in poll.error


def test_secrets_come_from_the_environment_before_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "obehy.local.toml"
    config.write_text('schema_version = 1\n[realtime.secrets]\ngolemio = "from-file"\n', "utf-8")
    monkeypatch.delenv("OBEHY_SECRET_GOLEMIO", raising=False)
    assert load_secrets(["golemio", "other"], config) == {"golemio": "from-file"}
    monkeypatch.setenv("OBEHY_SECRET_GOLEMIO", "from-env")
    assert load_secrets(["golemio"], config) == {"golemio": "from-env"}
