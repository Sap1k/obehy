"""Proxied, anonymous egress (docs/R2_SLICE.md section 7).

SŽ is reported to IP-ban addresses that run services against it, so a channel declaring
`egress = "<name>"` is only ever sent through that egress's proxy pool, with no User-Agent and
no header but the manifest's own. The pool is a proxy list (Webshare's download format, one
`ip:port:username:password` per line) from secret configuration: a saved file
(`OBEHY_EGRESS_<NAME>_PROXY_LIST_FILE` or `[realtime.egress.<name>] proxy_list_file`), else a
download URL (`..._PROXY_LIST_URL` / `proxy_list_url`; Webshare regenerates these links, so a
saved list is the steadier choice). Either is re-read every `refresh_s`. The URL, the file's
contents and the proxies' addresses and credentials are never logged: a proxy is named by its
index in the list.

Requests go round-robin over the pool. A proxy answering 403 or 429, or failing to connect, is
benched for `cooldown_s` and the request is retried once on the next proxy. A channel whose
egress is not configured never polls (fail closed).
"""

from __future__ import annotations

import os
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from urllib.request import OpenerDirector, ProxyHandler, build_opener, urlopen

from obehy.realtime.archive import Poll
from obehy.realtime.manifest import Channel
from obehy.realtime.policy import EgressPolicy
from obehy.realtime.runtime.fetch import Clock, channel_request, fetch, refused, send, utc_clock
from obehy.runtime_config import ConfigurationError, default_config_path

BENCH_STATUSES = frozenset({403, 407, 429})
LIST_TIMEOUT_S = 30.0


class EgressError(RuntimeError):
    """An egress cannot be used (no pool, an unusable proxy list)."""


FILE = "file:"  # a source that is a saved list, not a URL


def _env_name(name: str, kind: str = "URL") -> str:
    return f"OBEHY_EGRESS_{name.upper().replace('-', '_')}_PROXY_LIST_{kind}"


def _setting(name: str, table: dict[str, Any], kind: str, key: str) -> str:
    value = os.environ.get(_env_name(name, kind), "").strip()
    raw = table.get(key)
    return value or (raw.strip() if isinstance(raw, str) else "")


def load_egress_urls(names: Sequence[str], path: Path | None = None) -> dict[str, str]:
    """The proxy-list source of each configured egress: `file:<path>` for a saved list (it wins
    over a URL), else the download URL; the environment before the config file."""

    found: dict[str, str] = {}
    document: dict[str, Any] = {}
    source = (path or default_config_path()).resolve()
    if source.is_file():
        try:
            with source.open("rb") as stream:
                document = tomllib.load(stream)
        except tomllib.TOMLDecodeError as error:
            raise ConfigurationError(f"Invalid TOML in {source}: {error}") from error
    realtime = document.get("realtime")
    egress = cast(dict[str, Any], realtime).get("egress") if isinstance(realtime, dict) else None
    for name in sorted(set(names)):
        table: dict[str, Any] = {}
        if isinstance(egress, dict):
            raw_table = cast(dict[str, Any], egress).get(name)
            table = cast(dict[str, Any], raw_table) if isinstance(raw_table, dict) else {}

        saved = _setting(name, table, "FILE", "proxy_list_file")
        url = _setting(name, table, "URL", "proxy_list_url")
        if saved:
            found[name] = FILE + os.path.expandvars(saved)
        elif url:
            found[name] = url
    return found


def parse_proxy_list(text: str) -> tuple[str, ...]:
    """Proxy URLs from `ip:port:username:password` lines; malformed lines are skipped."""

    proxies: list[str] = []
    for line in text.splitlines():
        parts = line.strip().split(":")
        if len(parts) != 4 or not all(parts) or not parts[1].isdigit():
            continue
        host, port, user, password = parts
        proxies.append(f"http://{quote(user, safe='')}:{quote(password, safe='')}@{host}:{port}")
    return tuple(proxies)


def _download_list(url: str) -> str:
    if url.startswith(FILE):
        return Path(url.removeprefix(FILE)).read_text(encoding="utf-8")
    with urlopen(url, timeout=LIST_TIMEOUT_S) as response:
        return cast(bytes, response.read()).decode("utf-8", "replace")


def anonymous_opener(proxy_url: str) -> OpenerDirector:
    """An opener through one proxy that sends no User-Agent (urllib adds one by default)."""

    opener = build_opener(ProxyHandler({"http": proxy_url, "https": proxy_url}))
    opener.addheaders = []
    return opener


@dataclass
class ProxyPool:
    """One egress's proxies; thread-safe, since channels are fetched in worker threads."""

    name: str
    list_url: str
    policy: EgressPolicy
    download: Callable[[str], str] = _download_list
    monotonic: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = print
    proxies: tuple[str, ...] = ()
    benched_until: dict[int, float] = field(default_factory=dict[int, float])
    _next: int = 0
    _refreshed_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def refresh(self) -> None:
        """Reload the list when due; a failed or empty download keeps the current pool."""

        now = self.monotonic()
        if self._refreshed_at is not None and now - self._refreshed_at < self.policy.refresh_s:
            return
        self._refreshed_at = now
        try:
            proxies = parse_proxy_list(self.download(self.list_url))
        except (OSError, ValueError) as error:
            # Never the URL or the error text: both may carry the secret.
            code = getattr(error, "code", None)
            detail = type(error).__name__ + (f" {code}" if isinstance(code, int) else "")
            self.log(f"egress {self.name}: proxy list download failed ({detail})")
            return
        if not proxies:
            self.log(f"egress {self.name}: proxy list has no usable line; pool kept")
            return
        if proxies != self.proxies:
            self.proxies = proxies
            self.benched_until.clear()
            self._next = 0
            self.log(f"egress {self.name}: {len(proxies)} proxies")

    def pick(self) -> tuple[int, str] | None:
        """The next proxy not on the bench, round-robin; None if every one is benched."""

        with self._lock:
            self.refresh()
            now = self.monotonic()
            count = len(self.proxies)
            for step in range(count):
                index = (self._next + step) % count
                if self.benched_until.get(index, 0.0) <= now:
                    self._next = (index + 1) % count
                    return index, self.proxies[index]
            return None

    def bench(self, index: int, reason: str) -> None:
        with self._lock:
            self.benched_until[index] = self.monotonic() + self.policy.cooldown_s
        self.log(f"egress {self.name}: proxy #{index} benched ({reason})")


def _bench_reason(poll: Poll) -> str | None:
    if poll.status in BENCH_STATUSES:
        return f"HTTP {poll.status}"
    if poll.status is None and poll.error is not None:
        return poll.error.split(":", 1)[0]
    return None


@dataclass
class Egress:
    """The fetcher of the recorder and the worker: direct, or through a channel's pool."""

    pools: Mapping[str, ProxyPool]
    clock: Clock = utc_clock
    opener: Callable[[str], OpenerDirector] = anonymous_opener

    def usable(self, channels: Sequence[Channel], log: Callable[[str], None]) -> list[Channel]:
        """The channels that may poll: an egress channel needs its pool (fail closed)."""

        kept: list[Channel] = []
        for channel in channels:
            if channel.egress is not None and channel.egress not in self.pools:
                log(
                    f"{channel.name}: egress {channel.egress!r} has no proxy list "
                    f"({_env_name(channel.egress, 'FILE')}, {_env_name(channel.egress)} or "
                    f"[realtime.egress.{channel.egress}]); not polled"
                )
                continue
            kept.append(channel)
        return kept

    def fetch(self, channel: Channel, body: bytes | None = None) -> Poll:
        if channel.egress is None:
            return fetch(channel, self.clock)
        pool = self.pools.get(channel.egress)
        if pool is None:
            return refused(channel, f"egress {channel.egress!r} is not configured", self.clock)
        poll: Poll | None = None
        for _attempt in range(2):
            picked = pool.pick()
            if picked is None:
                break
            index, proxy = picked
            # A fresh request per attempt: the proxy handler rewrites the one it sends.
            request = channel_request(channel, anonymous=True, body=body)
            poll = send(request, channel.timeout_s, self.clock, self.opener(proxy))
            reason = _bench_reason(poll)
            if reason is None:
                return poll
            pool.bench(index, reason)
        if poll is None:
            state = "every proxy benched" if pool.proxies else "no proxy list"
            return refused(channel, f"egress {channel.egress!r}: {state}", self.clock)
        return poll


def build_egress(
    channels: Sequence[Channel],
    policy: EgressPolicy,
    *,
    config: Path | None = None,
    log: Callable[[str], None] = print,
) -> Egress:
    names = sorted({c.egress for c in channels if c.egress is not None})
    urls = load_egress_urls(names, config)
    pools = {name: ProxyPool(name, url, policy, log=log) for name, url in urls.items()}
    for pool in pools.values():
        pool.refresh()
    return Egress(pools)
