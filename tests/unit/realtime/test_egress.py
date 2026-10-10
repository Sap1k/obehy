"""Proxied, anonymous egress (docs/R2_SLICE.md section 7) against local stub proxies."""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from obehy.realtime.archive import Poll
from obehy.realtime.manifest import Channel
from obehy.realtime.policy import EgressPolicy
from obehy.realtime.runtime.egress import (
    Egress,
    ProxyPool,
    load_egress_urls,
    parse_proxy_list,
)
from obehy.realtime.runtime.fetch import fetch
from obehy.realtime.runtime.scheduler import backoff_interval, failures_after

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
    pool = ProxyPool("sz", "https://lists.invalid/secret", POLICY, download=lambda _: text)
    pool.log = logs.append
    pool.refresh()
    return pool


def test_proxy_list_lines_become_proxy_urls() -> None:
    text = "10.0.0.1:8080:alice:s3cr:et\n10.0.0.2:8080:bob:p@ss\n\nnot a proxy\n10.0.0.3:x:a:b\n"
    assert parse_proxy_list(text) == ("http://bob:p%40ss@10.0.0.2:8080",)


def test_egress_url_comes_from_the_environment_before_the_config(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    config = tmp_path / "obehy.local.toml"
    config.write_text(
        'schema_version = 1\n[realtime.egress.sz]\nproxy_list_url = "https://config.invalid/x"\n',
        encoding="utf-8",
    )
    monkeypatch.delenv("OBEHY_EGRESS_SZ_PROXY_LIST_URL", raising=False)
    assert load_egress_urls(["sz", "other"], config) == {"sz": "https://config.invalid/x"}
    monkeypatch.setenv("OBEHY_EGRESS_SZ_PROXY_LIST_URL", "https://env.invalid/y")
    assert load_egress_urls(["sz"], config) == {"sz": "https://env.invalid/y"}


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
        "https://lists.invalid/secret",
        POLICY,
        download=lambda _: next(texts),
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


def test_a_saved_proxy_list_wins_over_the_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    saved = tmp_path / "proxies.txt"
    saved.write_text("10.0.0.1:8080:alice:secret\n", encoding="utf-8")
    config = tmp_path / "obehy.local.toml"
    config.write_text(
        "schema_version = 1\n[realtime.egress.sz]\n"
        'proxy_list_url = "https://config.invalid/x"\n'
        f'proxy_list_file = "{saved.as_posix()}"\n',
        encoding="utf-8",
    )
    for kind in ("URL", "FILE"):
        monkeypatch.delenv(f"OBEHY_EGRESS_SZ_PROXY_LIST_{kind}", raising=False)
    (source,) = load_egress_urls(["sz"], config).values()
    pool = ProxyPool("sz", source, POLICY, log=lambda _: None)
    pool.refresh()
    assert pool.proxies == ("http://alice:secret@10.0.0.1:8080",)
