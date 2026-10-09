from __future__ import annotations

import hashlib
import io
import json
import tarfile
from dataclasses import replace
from pathlib import Path
from typing import IO, Any

import pytest

from obehy.production_package import package_digest
from obehy.release.fetch import (
    API,
    FetchError,
    Published,
    fetch_release,
    find_release,
    prune,
)

RUN = "20261009T030215Z-8cf199243726"
OLDER = "20261008T204357Z-675f7bb33875"


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _package(root: Path, name: str) -> dict[str, str]:
    package = root / name
    (package / "serving").mkdir(parents=True)
    trip = b"trip_id\n1\n"
    (package / "serving" / "trip.csv").write_bytes(trip)
    (package / "gtfs.zip").write_bytes(b"zip")
    files = [{"path": "serving/trip.csv", "size_bytes": len(trip), "sha256": _sha(trip)}]
    (package / "manifest.json").write_text(json.dumps({"files": files}), encoding="utf-8")
    return {
        "manifest_sha256": _sha((package / "manifest.json").read_bytes()),
        "package_sha256": package_digest(package),
    }


def _tar(root: Path, name: str) -> bytes:
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w") as archive:
        archive.add(root / name, arcname=name)
    return buffer.getvalue()


class _GitHub:
    """Serves releases from memory the way api.github.com and its asset storage do."""

    def __init__(self) -> None:
        self.bodies: dict[str, bytes] = {}
        self.releases: list[dict[str, Any]] = []
        self.downloads: list[str] = []

    def publish(self, tag: str, assets: dict[str, bytes], **flags: bool) -> None:
        listed: list[dict[str, Any]] = []
        for name, body in assets.items():
            url = f"https://github.test/{tag}/{name}"
            self.bodies[url] = body
            listed.append(
                {
                    "name": name,
                    "browser_download_url": url,
                    "size": len(body),
                    "digest": f"sha256:{_sha(body)}",
                }
            )
        self.releases.append({"tag_name": tag, "assets": listed, **flags})

    def __call__(self, url: str, headers: dict[str, str]) -> IO[bytes]:
        if url.startswith(f"{API}/repos/o/r/releases/tags/"):
            tag = url.rsplit("/", 1)[1]
            body = json.dumps(next(r for r in self.releases if r["tag_name"] == tag)).encode()
        elif url.startswith(f"{API}/repos/o/r/releases"):
            body = json.dumps(self.releases).encode()
        else:
            self.downloads.append(url.rsplit("/", 1)[1])
            body = self.bodies[url]
        return io.BytesIO(body)


def _release(tmp_path: Path, run: str = RUN) -> dict[str, bytes]:
    built = tmp_path / "built" / run
    packages = {name: _package(built, name) for name in ("jdf", "czptt")}
    release = {"schema_version": 1, "run_id": run, "packages": packages}
    return {
        "release.json": json.dumps(release).encode(),
        "jdf.tar": _tar(built, "jdf"),
        "czptt.tar": _tar(built, "czptt"),
    }


def test_fetches_the_newest_build_and_skips_it_once_present(tmp_path: Path) -> None:
    github = _GitHub()
    github.publish(f"build-{OLDER}", _release(tmp_path, OLDER))
    github.publish(f"build-{RUN}", _release(tmp_path))
    github.publish("build-20261010T000000Z-000000000000", {}, draft=True)
    github.publish("v1.0", {})
    into = tmp_path / "releases"

    published = find_release("o/r", None, github)
    assert published.run_id == RUN
    directory, new = fetch_release(published, into, github, report=lambda _: None)

    assert (directory, new) == (into / RUN, True)
    assert (directory / "jdf" / "serving" / "trip.csv").read_bytes() == b"trip_id\n1\n"
    assert json.loads((directory / "release.json").read_text(encoding="utf-8"))["run_id"] == RUN
    assert sorted(path.name for path in into.iterdir()) == [RUN]

    github.downloads.clear()
    assert fetch_release(published, into, github, report=lambda _: None) == (directory, False)
    assert github.downloads == []
    assert find_release("o/r", OLDER, github).run_id == OLDER


def _fails(tmp_path: Path, assets: dict[str, bytes], match: str, **change: Any) -> None:
    github = _GitHub()
    github.publish(f"build-{RUN}", assets)
    published = find_release("o/r", None, github)
    if change:
        assets_ = tuple(
            replace(asset, **change) if asset.name == "jdf.tar" else asset
            for asset in published.assets
        )
        published = Published(published.run_id, assets_)
    into = tmp_path / "releases"
    with pytest.raises(FetchError, match=match):
        fetch_release(published, into, github, report=lambda _: None)
    assert not into.exists() or list(into.iterdir()) == []  # nothing half-unpacked stays


def test_an_asset_unlike_its_github_digest_is_rejected(tmp_path: Path) -> None:
    _fails(tmp_path, _release(tmp_path), r"jdf\.tar does not match", sha256="0" * 64)


def test_a_package_unlike_its_manifest_is_rejected(tmp_path: Path) -> None:
    assets = _release(tmp_path)
    (tmp_path / "built" / RUN / "jdf" / "serving" / "trip.csv").write_bytes(b"trip_id\n2\n")
    assets["jdf.tar"] = _tar(tmp_path / "built" / RUN, "jdf")
    _fails(tmp_path, assets, r"jdf: serving/trip\.csv does not match its manifest")


def test_a_tar_reaching_outside_its_package_is_rejected(tmp_path: Path) -> None:
    assets = _release(tmp_path)
    assets["jdf.tar"] = _tar(tmp_path / "built" / RUN, "czptt")
    _fails(tmp_path, assets, r"czptt lies outside jdf/")


def test_release_json_of_another_run_is_rejected(tmp_path: Path) -> None:
    assets = _release(tmp_path)
    assets["release.json"] = _release(tmp_path / "other", OLDER)["release.json"]
    _fails(tmp_path, assets, f"does not describe run {RUN}")


def test_prune_keeps_the_newest_and_the_fetched_run(tmp_path: Path) -> None:
    runs = [f"2026100{day}T000000Z-000000000000" for day in range(1, 6)]
    for run in runs:
        (tmp_path / run).mkdir()
    (tmp_path / "notes").mkdir()

    assert prune(tmp_path, 2, protect=runs[0]) == [runs[2], runs[1]]
    assert sorted(path.name for path in tmp_path.iterdir()) == [runs[0], runs[3], runs[4], "notes"]


def test_prune_keeps_the_release_the_active_link_points_at(tmp_path: Path) -> None:
    runs = [f"2026100{day}T000000Z-000000000000" for day in range(1, 5)]
    for run in runs:
        (tmp_path / run).mkdir()
    try:
        (tmp_path / "active").symlink_to(runs[0], target_is_directory=True)
    except OSError:
        pytest.skip("this system does not allow symbolic links")

    assert prune(tmp_path, 1, protect=runs[3]) == [runs[2], runs[1]]
