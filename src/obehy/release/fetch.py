"""``obehy release fetch``: download a published release, verify it and unpack it.

The nightly build (``.github/workflows/build.yml``) publishes each release as a GitHub
release tagged ``build-<run-id>``: ``release.json`` plus one tar per package directory. Every
asset is checked against the size and digest GitHub records for it, ``release.json`` against
the tag, and every package against ``release.json`` and its own manifest (``release load``
repeats the relation checks). The release is unpacked into a hidden sibling and renamed into
``<into>/<run-id>`` only once it verifies, so a release directory that exists is complete.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tarfile
import urllib.request
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import IO, Any, cast

from obehy.pipeline.files import file_digest
from obehy.production_package import package_digest

DEFAULT_REPOSITORY = "Sap1k/obehy"
DEFAULT_KEEP = 3
TAG_PREFIX = "build-"
API = "https://api.github.com"
TOKEN_ENV = ("GITHUB_TOKEN", "GH_TOKEN")
_RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{12}$")
_CHUNK = 1024 * 1024

Report = Callable[[str], None]
# GET a URL with the given headers and return the response body as a stream.
Opener = Callable[[str, dict[str, str]], IO[bytes]]


class FetchError(RuntimeError):
    """A release cannot be found, downloaded or verified."""


@dataclass(frozen=True)
class Asset:
    name: str
    url: str
    size: int
    sha256: str


@dataclass(frozen=True)
class Published:
    run_id: str
    assets: tuple[Asset, ...]

    def asset(self, name: str) -> Asset | None:
        return next((asset for asset in self.assets if asset.name == name), None)


def http_open(url: str, headers: dict[str, str]) -> IO[bytes]:
    request = urllib.request.Request(url, headers={"User-Agent": "obehy-release-fetch"})
    for name, value in headers.items():
        # Unredirected: a token for api.github.com must not follow a redirect to storage.
        request.add_unredirected_header(name, value)
    return cast(IO[bytes], urllib.request.urlopen(request, timeout=60))


def _token_headers() -> dict[str, str]:
    token = next((os.environ[name] for name in TOKEN_ENV if os.environ.get(name)), None)
    return {"Authorization": f"Bearer {token}"} if token else {}


def _api(opener: Opener, path: str) -> Any:
    headers = {"Accept": "application/vnd.github+json", **_token_headers()}
    try:
        with opener(f"{API}{path}", headers) as response:
            return json.load(response)
    except (OSError, json.JSONDecodeError) as error:
        raise FetchError(f"GitHub API request {path} failed: {error}") from error


def _published(release: dict[str, Any]) -> Published:
    tag = cast(str, release["tag_name"])
    run_id = tag.removeprefix(TAG_PREFIX)
    if not tag.startswith(TAG_PREFIX) or not _RUN_ID.match(run_id):
        raise FetchError(f"Release tag {tag!r} does not name a build run")
    assets: list[Asset] = []
    for asset in cast(list[dict[str, Any]], release.get("assets", [])):
        digest = cast(str | None, asset.get("digest"))
        if not digest or not digest.startswith("sha256:"):
            raise FetchError(f"{tag}: asset {asset['name']} has no sha256 digest")
        assets.append(
            Asset(
                name=cast(str, asset["name"]),
                url=cast(str, asset["browser_download_url"]),
                size=int(asset["size"]),
                sha256=digest.removeprefix("sha256:"),
            )
        )
    return Published(run_id, tuple(assets))


def find_release(repository: str, run_id: str | None, opener: Opener = http_open) -> Published:
    """The given run's release, or the newest published build."""

    if run_id is not None:
        return _published(_api(opener, f"/repos/{repository}/releases/tags/{TAG_PREFIX}{run_id}"))
    releases = cast(list[dict[str, Any]], _api(opener, f"/repos/{repository}/releases?per_page=30"))
    builds = [
        release
        for release in releases
        if not release.get("draft")
        and not release.get("prerelease")
        and cast(str, release.get("tag_name", "")).startswith(TAG_PREFIX)
    ]
    if not builds:
        raise FetchError(f"{repository} has no published build release")
    return _published(max(builds, key=lambda release: cast(str, release["tag_name"])))


class _Hashing:
    """A read-through stream that hashes and counts what passes."""

    def __init__(self, stream: IO[bytes]) -> None:
        self._stream = stream
        self.digest = hashlib.sha256()
        self.size = 0

    def read(self, size: int = -1) -> bytes:
        data = self._stream.read(size)
        self.digest.update(data)
        self.size += len(data)
        return data

    def drain(self) -> None:
        while self.read(_CHUNK):
            pass


def _check(asset: Asset, stream: _Hashing) -> None:
    stream.drain()
    if stream.size != asset.size or stream.digest.hexdigest() != asset.sha256:
        raise FetchError(f"{asset.name} does not match the size and digest GitHub records")


def _download_json(asset: Asset, opener: Opener) -> tuple[bytes, dict[str, Any]]:
    with opener(asset.url, {}) as response:
        stream = _Hashing(response)
        body = stream.read()
        _check(asset, stream)
    try:
        value = json.loads(body)
    except json.JSONDecodeError as error:
        raise FetchError(f"{asset.name} is not JSON") from error
    if not isinstance(value, dict):
        raise FetchError(f"{asset.name} is not a JSON object")
    return body, cast(dict[str, Any], value)


def _unpack(asset: Asset, opener: Opener, into: Path) -> None:
    """Stream a package tar into ``into``; it must hold one directory named after the asset."""

    top = asset.name.removesuffix(".tar")
    with opener(asset.url, {}) as response:
        stream = _Hashing(response)
        with tarfile.open(fileobj=cast(IO[bytes], stream), mode="r|") as archive:
            for member in cast(Iterable[tarfile.TarInfo], archive):
                if member.name != top and not member.name.startswith(f"{top}/"):
                    raise FetchError(f"{asset.name}: {member.name} lies outside {top}/")
                archive.extract(member, into, filter="data")
        _check(asset, stream)


def _verify_files(root: Path, expected: dict[str, str], label: str) -> None:
    for relative, sha256 in expected.items():
        path = root / relative
        if not path.is_file() or file_digest(path) != sha256:
            raise FetchError(f"{label}: {relative} is missing or does not match release.json")


def _verify_package(root: Path, name: str, entry: dict[str, Any]) -> None:
    package = root / name
    _verify_files(package, {"manifest.json": cast(str, entry.get("manifest_sha256"))}, name)
    try:
        manifest = json.loads((package / "manifest.json").read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise FetchError(f"{name}: manifest.json is not JSON") from error
    for file in cast(list[dict[str, Any]], manifest.get("files", [])):
        path = package / cast(str, file["path"])
        if (
            not path.is_file()
            or path.stat().st_size != file["size_bytes"]
            or file_digest(path) != file["sha256"]
        ):
            raise FetchError(f"{name}: {file['path']} does not match its manifest")
    if package_digest(package) != entry.get("package_sha256"):
        raise FetchError(f"{name}: package contents do not match release.json")


def verify_release(root: Path, release: dict[str, Any]) -> None:
    """Check an unpacked release against the hashes ``release.json`` records."""

    for name, entry in cast(dict[str, dict[str, Any]], release.get("packages", {})).items():
        _verify_package(root, name, entry)
    filtered = cast(dict[str, Any] | None, release.get("outputs", {}).get("jdf_filtered"))
    if filtered is not None:
        _verify_files(
            root / "jdf-filtered",
            {
                "gtfs.zip": cast(str, filtered["gtfs_sha256"]),
                "filter-report.json": cast(str, filtered["filter_report_sha256"]),
            },
            "jdf-filtered",
        )


def fetch_release(
    published: Published, into: Path, opener: Opener = http_open, report: Report = print
) -> tuple[Path, bool]:
    """Download, verify and unpack a release; returns its directory and whether it is new."""

    final = into / published.run_id
    if (final / "release.json").is_file():
        return final, False
    manifest = published.asset("release.json")
    if manifest is None:
        raise FetchError(f"{TAG_PREFIX}{published.run_id} has no release.json")
    body, release = _download_json(manifest, opener)
    if release.get("schema_version") != 1 or release.get("run_id") != published.run_id:
        raise FetchError(f"release.json does not describe run {published.run_id}")
    tars = [asset for asset in published.assets if asset.name.endswith(".tar")]
    missing = [
        name for name in release.get("packages", {}) if published.asset(f"{name}.tar") is None
    ]
    if missing:
        raise FetchError(f"{TAG_PREFIX}{published.run_id} lacks the {', '.join(missing)} tar")

    staging = into / f".{published.run_id}.partial"
    shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        for asset in tars:
            report(f"downloading {asset.name} ({asset.size / 1e6:.0f} MB)")
            _unpack(asset, opener, staging)
        report("verifying packages")
        verify_release(staging, release)
        (staging / "release.json").write_bytes(body)
        staging.rename(final)
    except BaseException:
        shutil.rmtree(staging, ignore_errors=True)
        raise
    return final, True


def prune(into: Path, keep: int, protect: str) -> list[str]:
    """Delete fetched releases beyond the ``keep`` newest; never ``protect``."""

    runs = sorted(
        (path.name for path in into.iterdir() if path.is_dir() and _RUN_ID.match(path.name)),
        reverse=True,
    )
    dropped = [run for run in runs[keep:] if run != protect]
    for run in dropped:
        shutil.rmtree(into / run)
    return dropped
