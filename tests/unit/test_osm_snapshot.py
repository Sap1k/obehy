from __future__ import annotations

import hashlib
import subprocess
from collections.abc import Sequence
from pathlib import Path
from urllib.request import Request

import pytest

from obehy import osm_snapshot
from obehy.osm_snapshot import (
    OsmSnapshotError,
    build_snapshot,
    validate_snapshot,
)
from obehy.runtime_config import JrUtilRuntime, RuntimeConfig


class _Response:
    def __init__(self, payload: bytes) -> None:
        self.payload = payload

    def read(self, _size: int = -1) -> bytes:
        payload, self.payload = self.payload, b""
        return payload

    def __enter__(self) -> _Response:
        return self

    def __exit__(self, *_args: object) -> None:
        return


class _FailingResponse(_Response):
    def read(self, _size: int = -1) -> bytes:
        if self.payload:
            payload, self.payload = self.payload, b""
            return payload
        raise OSError("fixture interrupted download")


def _config(tmp_path: Path) -> RuntimeConfig:
    return RuntimeConfig(
        source=tmp_path / "config.toml",
        workdir=tmp_path / "work",
        osm_file=tmp_path / "active" / "region.osm.pbf",
        jrunify_ext_geodata_dir=tmp_path / "geodata",
        jrutil=JrUtilRuntime(directory=tmp_path / "jrutil", command=None),
    )


def test_build_reuses_extracts_and_active_merge(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    payloads = {region: f"pbf:{region}:v1".encode() for region in osm_snapshot.REGIONS}
    pbf_downloads: list[str] = []
    merge_calls: list[list[bytes]] = []

    def fetch(url: str) -> bytes:
        region = next(region for region in osm_snapshot.REGIONS if region in url)
        digest = hashlib.md5(payloads[region]).hexdigest()
        return f"{digest}  source.osm.pbf\n".encode()

    def open_url(request: Request, timeout: int) -> _Response:
        assert timeout == 120
        url = str(request.full_url)
        region = next(region for region in osm_snapshot.REGIONS if region in url)
        pbf_downloads.append(region)
        return _Response(payloads[region])

    def merge(inputs: Sequence[Path], output: Path) -> dict[str, object]:
        values = [path.read_bytes() for path in inputs]
        merge_calls.append(values)
        output.write_bytes(b"\n".join(values))
        return {"engine": "fixture"}

    def node_filter(
        source: Path,
        destination: Path,
        *,
        source_key: str | None = None,
    ) -> Path:
        assert source == config.osm_file.resolve()
        assert source_key is not None
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(destination.name.encode())
        return destination

    monkeypatch.setattr(osm_snapshot, "urlopen", open_url)
    config = _config(tmp_path)

    first = build_snapshot(
        config,
        fetch=fetch,
        merge=merge,
        railway_filter=node_filter,
        transit_filter=node_filter,
    )
    second = build_snapshot(
        config,
        fetch=fetch,
        merge=merge,
        railway_filter=node_filter,
        transit_filter=node_filter,
    )

    assert first == second == config.osm_file
    assert pbf_downloads == list(osm_snapshot.REGIONS)
    assert len(merge_calls) == 1
    manifest = validate_snapshot(config.osm_file, config.workdir, full_hash=True)
    assert manifest["regions"] == list(osm_snapshot.REGIONS)
    assert manifest["attribution"].startswith("© OpenStreetMap contributors")

    changed_region = "austria"
    payloads[changed_region] = b"pbf:austria:v2"
    build_snapshot(
        config,
        fetch=fetch,
        merge=merge,
        railway_filter=node_filter,
        transit_filter=node_filter,
    )
    assert pbf_downloads.count(changed_region) == 2
    assert all(
        pbf_downloads.count(region) == (2 if region == changed_region else 1)
        for region in osm_snapshot.REGIONS
    )
    assert len(merge_calls) == 2


def test_validation_rejects_modified_active_file(tmp_path: Path) -> None:
    config = _config(tmp_path)
    config.osm_file.parent.mkdir(parents=True)
    config.osm_file.write_bytes(b"valid")
    manifest = {
        "schema_version": 2,
        "merge_key": "fixture-key",
        "output": {
            "bytes": 5,
            "sha256": hashlib.sha256(b"valid").hexdigest(),
        },
        "active_file_stats": {
            "bytes": 5,
            "mtime_ns": config.osm_file.stat().st_mtime_ns,
        },
    }
    osm_snapshot.write_json(osm_snapshot.active_manifest_path(config.osm_file), manifest)
    config.osm_file.write_bytes(b"wrong")

    with pytest.raises(OsmSnapshotError, match="hash differs"):
        validate_snapshot(config.osm_file, config.workdir, full_hash=True)


def test_changed_extract_download_failure_keeps_previous_cache(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    region = "austria"
    payload = b"version-one"

    def fetch(_url: str) -> bytes:
        return f"{hashlib.md5(payload).hexdigest()}  source.osm.pbf\n".encode()

    def successful_open(_request: Request, timeout: int) -> _Response:
        assert timeout == 120
        return _Response(payload)

    monkeypatch.setattr(
        osm_snapshot,
        "urlopen",
        successful_open,
    )
    first = osm_snapshot._download_extract(  # pyright: ignore[reportPrivateUsage]
        region, tmp_path / "extracts", fetch
    )
    first_path = Path(first.path)

    payload = b"version-two"

    def failing_open(_request: Request, timeout: int) -> _FailingResponse:
        assert timeout == 120
        return _FailingResponse(b"partial")

    monkeypatch.setattr(
        osm_snapshot,
        "urlopen",
        failing_open,
    )
    with pytest.raises(OSError, match="interrupted"):
        osm_snapshot._download_extract(  # pyright: ignore[reportPrivateUsage]
            region, tmp_path / "extracts", fetch
        )

    assert first_path.read_bytes() == b"version-one"
    assert not list((tmp_path / "extracts").rglob("*.part"))


def test_missing_native_osmium_has_installation_guidance(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def missing(_name: str) -> None:
        return None

    monkeypatch.setattr(osm_snapshot.shutil, "which", missing)

    with pytest.raises(OsmSnapshotError, match="osmium-tool"):
        osm_snapshot._discover_osmium()  # pyright: ignore[reportPrivateUsage]


def test_native_osmium_merge_uses_progress(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    inputs = [tmp_path / "first.osm.pbf", tmp_path / "second.osm.pbf"]
    for index, path in enumerate(inputs):
        path.write_bytes(f"input-{index}".encode())
    output = tmp_path / "output.osm.pbf"
    commands: list[list[str]] = []

    def run(
        command: Sequence[str],
        *,
        check: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert not check
        assert text
        values = list(command)
        commands.append(values)
        assert "merge" in values
        target = Path(values[values.index("--output") + 1])
        target.write_bytes(b"native-output")
        return subprocess.CompletedProcess(values, 0, "")

    monkeypatch.setattr(osm_snapshot.subprocess, "run", run)
    runtime = osm_snapshot.OsmiumRuntime(("osmium",), "native", "osmium fixture")

    details = osm_snapshot._merge_with_osmium(  # pyright: ignore[reportPrivateUsage]
        inputs,
        output,
        runtime,
    )

    assert output.read_bytes() == b"native-output"
    assert details["engine"] == "osmium-tool"
    merge_command = next(command for command in commands if "merge" in command)
    assert "--progress" in merge_command
    assert "--verbose" in merge_command
    assert "native merge complete" in capsys.readouterr().err


def test_osm_filters_are_single_pass_and_node_only(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    source = tmp_path / "source.osm.pbf"
    source.write_bytes(b"source")
    destination = osm_snapshot.railway_locations_path(tmp_path)
    commands: list[list[str]] = []
    runtime = osm_snapshot.OsmiumRuntime(("osmium",), "native", "osmium fixture")

    def discover() -> osm_snapshot.OsmiumRuntime:
        return runtime

    monkeypatch.setattr(osm_snapshot, "_discover_osmium", discover)

    def run(
        command: Sequence[str],
        *,
        check: bool,
        text: bool,
    ) -> subprocess.CompletedProcess[str]:
        assert not check
        assert text
        values = list(command)
        commands.append(values)
        target = Path(values[values.index("--output") + 1])
        target.write_bytes(b"filtered")
        return subprocess.CompletedProcess(values, 0, "")

    monkeypatch.setattr(osm_snapshot.subprocess, "run", run)

    result = osm_snapshot.filter_railway_locations(
        source,
        destination,
        source_key="fixture-source",
    )
    reused = osm_snapshot.filter_railway_locations(
        source,
        destination,
        source_key="fixture-source",
    )

    assert result == destination
    assert reused == destination
    assert osm_snapshot.validate_railway_locations(tmp_path, "fixture-source") == destination
    assert destination.read_bytes() == b"filtered"
    assert len(commands) == 1
    railway_command = commands[0]
    assert "tags-filter" in railway_command
    assert "--omit-referenced" in railway_command
    assert "n/railway=station,halt,stop" in railway_command
    assert not any("public_transport" in argument for argument in railway_command)

    transit_destination = osm_snapshot.jdf_transit_stops_path(tmp_path)
    transit = osm_snapshot.filter_jdf_transit_stops(
        source,
        transit_destination,
        source_key="fixture-source",
    )
    transit_reused = osm_snapshot.filter_jdf_transit_stops(
        source,
        transit_destination,
        source_key="fixture-source",
    )
    assert transit == transit_reused == transit_destination
    assert (
        osm_snapshot.validate_jdf_transit_stops(tmp_path, "fixture-source") == transit_destination
    )
    assert len(commands) == 2
    transit_command = commands[1]
    assert "tags-filter" in transit_command
    assert "--omit-referenced" in transit_command
    assert "n/highway=bus_stop" in transit_command
    assert "n/public_transport=platform,pole,station" in transit_command
    assert "n/railway=tram_stop" in transit_command
    assert "n/amenity=bus_station" in transit_command
