"""Proxied, anonymous egress (docs/R2_SLICE.md section 7).

SŽ is reported to IP-ban addresses that run services against it, so a channel declaring
`proxied = true` (SŽ, ČD) is only ever sent through the proxy pool, with no User-Agent and no
header but the manifest's own. There is one pool for every such channel: a saved proxy list
(Webshare's format, one `ip:port:username:password` per line), a secret file named by
`OBEHY_EGRESS_PROXY_LIST_FILE` or `[realtime.egress] proxy_list_file` in the gitignored local
config, re-read every `refresh_s`. Webshare's download links are regenerated, so they are not
used. The file's contents and the proxies' addresses and credentials are never logged: a proxy
is named by its index in the list.

Requests go round-robin over the pool. A proxy answering 403 or 429, or failing to connect, is
benched for `cooldown_s` and the request is retried once on the next proxy. A proxied channel
never polls without the pool (fail closed).

`Egress` is the fetcher of every channel, so it also holds what a request needs beyond its
manifest: the secret of a channel with `auth` (an API key; such a channel without its secret
never polls either), and the cursor of a channel with `cursor` (delta polling: the last good
poll's header goes back as a query parameter, and is archived with the poll in `request`).
"""

from __future__ import annotations

import os
import threading
import time
import tomllib
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any, cast
from urllib.parse import quote
from urllib.request import HTTPSHandler, OpenerDirector, ProxyHandler, build_opener

from obehy.realtime.archive import Poll
from obehy.realtime.manifest import Channel
from obehy.realtime.policy import EgressPolicy
from obehy.realtime.runtime.fetch import (
    TLS,
    Clock,
    channel_request,
    fetch,
    refused,
    send,
    utc_clock,
)
from obehy.runtime_config import (
    ConfigurationError,
    default_config_path,
    load_secrets,
    secret_env,
)

BENCH_STATUSES = frozenset({403, 407, 429})


class EgressError(RuntimeError):
    """An egress cannot be used (no pool, an unusable proxy list)."""


LIST_ENV = "OBEHY_EGRESS_PROXY_LIST_FILE"
LIST_SETTING = "[realtime.egress] proxy_list_file"


def load_proxy_list(path: Path | None = None, log: Callable[[str], None] = print) -> str | None:
    """The saved proxy-list file; the environment before the config file. A per-source list of
    an older config (`OBEHY_EGRESS_SZ_PROXY_LIST_FILE`, `[realtime.egress.sz]`) is not read: it
    is named in the log, to be renamed."""

    document: dict[str, Any] = {}
    source = (path or default_config_path()).resolve()
    if source.is_file():
        try:
            with source.open("rb") as stream:
                document = tomllib.load(stream)
        except tomllib.TOMLDecodeError as error:
            raise ConfigurationError(f"Invalid TOML in {source}: {error}") from error
    realtime = document.get("realtime")
    raw = cast(dict[str, Any], realtime).get("egress") if isinstance(realtime, dict) else None
    table = cast(dict[str, Any], raw) if isinstance(raw, dict) else {}
    old = sorted(
        [k for k in os.environ if k.startswith("OBEHY_EGRESS_") and k != LIST_ENV]
        + [f"[realtime.egress.{k}]" for k, v in table.items() if isinstance(v, dict)]
    )
    if old:
        log(f"egress: {', '.join(old)} not read; the one list is {LIST_ENV} or {LIST_SETTING}")
    setting = table.get("proxy_list_file")
    saved = os.environ.get(LIST_ENV, "").strip() or (
        setting.strip() if isinstance(setting, str) else ""
    )
    return os.path.expandvars(saved) if saved else None


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


def _read_list(path: str) -> str:
    return Path(path).read_text(encoding="utf-8")


@lru_cache(maxsize=256)  # one per proxy, reused: building one per request is wasted work
def anonymous_opener(proxy_url: str) -> OpenerDirector:
    """An opener through one proxy that sends no User-Agent (urllib adds one by default)."""

    opener = build_opener(
        ProxyHandler({"http": proxy_url, "https": proxy_url}), HTTPSHandler(context=TLS)
    )
    opener.addheaders = []
    return opener


@dataclass
class ProxyPool:
    """The proxies; thread-safe, since channels are fetched in worker threads."""

    list_file: str
    policy: EgressPolicy
    read: Callable[[str], str] = _read_list
    monotonic: Callable[[], float] = time.monotonic
    log: Callable[[str], None] = print
    proxies: tuple[str, ...] = ()
    benched_until: dict[int, float] = field(default_factory=dict[int, float])
    _next: int = 0
    _refreshed_at: float | None = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def refresh(self) -> None:
        """Reread the list when due; an unreadable or empty file keeps the current pool."""

        now = self.monotonic()
        if self._refreshed_at is not None and now - self._refreshed_at < self.policy.refresh_s:
            return
        self._refreshed_at = now
        try:
            proxies = parse_proxy_list(self.read(self.list_file))
        except (OSError, ValueError) as error:
            # Never the error text: it may quote the secret file.
            code = getattr(error, "code", None)
            detail = type(error).__name__ + (f" {code}" if isinstance(code, int) else "")
            self.log(f"egress: proxy list unreadable ({detail})")
            return
        if not proxies:
            self.log("egress: proxy list has no usable line; pool kept")
            return
        if proxies != self.proxies:
            self.proxies = proxies
            self.benched_until.clear()
            self._next = 0
            self.log(f"egress: {len(proxies)} proxies")

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
        self.log(f"egress: proxy #{index} benched ({reason})")


def _bench_reason(poll: Poll) -> str | None:
    if poll.status in BENCH_STATUSES:
        return f"HTTP {poll.status}"
    if poll.status is None and poll.error is not None:
        return poll.error.split(":", 1)[0]
    return None


@dataclass
class Egress:
    """The fetcher of the recorder and the worker: direct, or through the pool."""

    pool: ProxyPool | None
    clock: Clock = utc_clock
    opener: Callable[[str], OpenerDirector] = anonymous_opener
    secrets: Mapping[str, str] = field(default_factory=dict[str, str])
    # Per channel: the cursor value of its last good poll and when that poll was received.
    cursors: dict[str, tuple[str, datetime]] = field(
        default_factory=dict[str, tuple[str, datetime]]
    )
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def usable(self, channels: Sequence[Channel], log: Callable[[str], None]) -> list[Channel]:
        """The channels that may poll: a proxied channel needs the pool, an auth channel its
        secret (fail closed)."""

        kept: list[Channel] = []
        for channel in channels:
            if channel.proxied and self.pool is None:
                log(f"{channel.name}: no proxy list ({LIST_ENV} or {LIST_SETTING}); not polled")
                continue
            if channel.auth is not None and channel.auth.secret not in self.secrets:
                log(
                    f"{channel.name}: secret {channel.auth.secret!r} is not configured "
                    f"({secret_env(channel.auth.secret)} or "
                    f"[realtime.secrets] {channel.auth.secret}); not polled"
                )
                continue
            kept.append(channel)
        return kept

    def _since(self, channel: Channel) -> str | None:
        if channel.cursor is None:
            return None
        with self._lock:
            last = self.cursors.get(channel.name)
        if last is None or self.clock() - last[1] > timedelta(seconds=channel.cursor.max_age_s):
            return None
        return last[0]

    def _advance(self, channel: Channel, poll: Poll) -> None:
        if channel.cursor is None or not poll.ok:
            return  # a failed poll keeps the cursor: the next delta covers the gap
        value = poll.headers.get(channel.cursor.header)
        with self._lock:
            if value:
                self.cursors[channel.name] = (value, poll.received_at)
            else:
                self.cursors.pop(channel.name, None)

    def fetch(self, channel: Channel, body: bytes | None = None) -> Poll:
        if not channel.proxied and channel.auth is None and channel.cursor is None:
            return fetch(channel, self.clock)
        headers: dict[str, str] = {}
        if channel.auth is not None:
            secret = self.secrets.get(channel.auth.secret)
            if secret is None:
                return refused(channel, f"secret {channel.auth.secret!r} required", self.clock)
            headers[channel.auth.header] = secret
        since = self._since(channel)
        params = {} if since is None or channel.cursor is None else {channel.cursor.param: since}
        keep = () if channel.cursor is None else (channel.cursor.header,)
        if not channel.proxied:
            request = channel_request(
                channel, anonymous=False, body=body, headers=headers, params=params
            )
            poll = send(request, channel.timeout_s, self.clock, keep=keep)
        else:
            poll = self._proxied(channel, body, headers, params, keep)
        self._advance(channel, poll)
        return replace(poll, request={**poll.request, **params}) if params else poll

    def _proxied(
        self,
        channel: Channel,
        body: bytes | None,
        headers: Mapping[str, str],
        params: Mapping[str, str],
        keep: tuple[str, ...],
    ) -> Poll:
        pool = self.pool
        if pool is None:
            return refused(channel, "proxied: no proxy list configured", self.clock)
        poll: Poll | None = None
        for _attempt in range(2):
            picked = pool.pick()
            if picked is None:
                break
            index, proxy = picked
            # A fresh request per attempt: the proxy handler rewrites the one it sends.
            request = channel_request(
                channel, anonymous=True, body=body, headers=headers, params=params
            )
            poll = send(request, channel.timeout_s, self.clock, self.opener(proxy), keep)
            reason = _bench_reason(poll)
            if reason is None:
                return poll
            pool.bench(index, reason)
        if poll is None:
            state = "every proxy benched" if pool.proxies else "no proxy list"
            return refused(channel, f"proxied: {state}", self.clock)
        return poll


def build_egress(
    channels: Sequence[Channel],
    policy: EgressPolicy,
    *,
    config: Path | None = None,
    log: Callable[[str], None] = print,
) -> Egress:
    pool: ProxyPool | None = None
    if any(c.proxied for c in channels):
        list_file = load_proxy_list(config, log)
        if list_file is not None:
            pool = ProxyPool(list_file, policy, log=log)
            pool.refresh()
    secrets = load_secrets((c.auth.secret for c in channels if c.auth is not None), config)
    return Egress(pool, secrets=secrets)
