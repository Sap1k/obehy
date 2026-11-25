# pyright: reportUnusedFunction=false
from __future__ import annotations

import json
import tomllib
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import replace
from datetime import date
from pathlib import Path
from typing import Any, cast

import pytest

from obehy import cli, gvd, national_jdf, route_shapes
from obehy.pipeline.download import DownloadError
from obehy.pipeline.errors import PipelineError
from obehy.runtime_config import JrUtilRuntime, RuntimeConfig

REFERENCE_DATE = date(2026, 12, 1)


def _posix(path: Path) -> str:
    return path.absolute().as_posix()


def _runtime(tmp_path: Path) -> RuntimeConfig:
    geodata = tmp_path / "geodata"
    (geodata / "other").mkdir(parents=True)
    (geodata / "other" / "municipalities.csv").write_text("Town,Stop,49,14,CZ\n", "utf-8")
    (geodata / "routes").mkdir()
    for name in ("transport-modes.csv", "presentation.csv"):
        (geodata / "routes" / name).write_text("header\n", encoding="utf-8")
    (geodata / "overlay").mkdir()
    (geodata / "overlay" / "pid-stop-overrides.csv").write_text("header\n", encoding="utf-8")
    (geodata / "filtered-jdf").mkdir()
    (geodata / "filtered-jdf" / "rules-v1.json").write_text("{}\n", encoding="utf-8")
    gazetteer = geodata / "gazetteer"
    gazetteer.mkdir()
    for name in cli.GAZETTEER_FILES:
        (gazetteer / name).write_bytes(b"")
    jrutil = tmp_path / "jrutil-rs"
    jrutil.mkdir()
    (jrutil / "Cargo.toml").write_text("[workspace]\n", encoding="utf-8")
    return RuntimeConfig(
        source=tmp_path / "obehy.toml",
        workdir=tmp_path / "work",
        artifact_root=tmp_path / "artifacts",
        osm_file=tmp_path / "osm.pbf",
        routing_cache_dir=tmp_path / "work" / "cache" / "routing",
        czptt_source_cache_dir=tmp_path / "work" / "cache" / "czptt-sources",
        jrunify_ext_geodata_dir=geodata,
        jrutil=JrUtilRuntime(directory=jrutil, command=None),
    )


def _package(path: Path, *, publishable: bool = True) -> None:
    path.mkdir(parents=True)
    with zipfile.ZipFile(path / "gtfs.zip", "w") as archive:
        archive.writestr("stops.txt", "stop_id,stop_lat,stop_lon,location_type,parent_station\n")
        archive.writestr("stop_times.txt", "trip_id,stop_id\n")
    (path / "serving").mkdir()
    (path / "diagnostics.json").write_text("{}\n", encoding="utf-8")
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "bundle_format": "jrutil-production",
                "bundle_version": 3,
                "serving_schema_version": "5.0",
                "contract_valid": True,
                "publication_eligible": publishable,
                "feed_version": "fixture",
                "compiler": {"tool": "jrutil-rs", "version": "fixture-version"},
            }
        ),
        encoding="utf-8",
    )


class _Compiler:
    """A fake command runner: `cargo build` does nothing, `jrutil build` writes what the
    specification names."""

    def __init__(self, *, publishable: bool = True) -> None:
        self.publishable = publishable
        self.commands: list[tuple[list[str], Path, Mapping[str, str] | None]] = []
        self.spec: dict[str, Any] = {}
        self.spec_text = ""

    def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
        _log: Path,
        _reporter: object,
        _progress: object,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self.commands.append((list(command), cwd, env))
        if "build" not in command or "--spec" not in command:
            return
        path = Path(command[command.index("--spec") + 1])
        self.spec_text = path.read_text(encoding="utf-8")
        self.spec = tomllib.loads(self.spec_text)
        jdf = cast(dict[str, Any], self.spec["jdf"])
        _package(Path(self.spec["czptt"]["output"]))
        _package(Path(jdf["output"]), publishable=self.publishable)
        if "filtered" in jdf:
            filtered = Path(jdf["filtered"]["output"])
            filtered.mkdir()
            (filtered / "gtfs.zip").write_bytes(b"fixture")
            (filtered / "filter-report.json").write_text("{}\n", encoding="utf-8")
        if "registry_candidates" in jdf:
            candidates = Path(jdf["registry_candidates"])
            candidates.parent.mkdir(parents=True)
            candidates.write_text("provisional_id\n", encoding="utf-8")
        reports = [Path(jdf["overlay"]["reports"]) / "overlay_place_candidates.csv"]
        if "posts" in jdf:
            reports.append(Path(jdf["posts"]["reports"]) / "post_candidates.csv")
        for report in reports:
            report.parent.mkdir(parents=True)
            report.write_text("candidate\n", encoding="utf-8")


def _downloader(_url: str, source_id: str, destination: Path, *, require_api: bool) -> Path:
    del require_api
    destination.write_bytes(b"fixture")
    descriptor = destination.with_name(f"{source_id}-descriptor.json")
    descriptor.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source_id": source_id,
                "retrieved_at": "2026-12-01T12:00:00+00:00",
                "source_uri": "https://example.invalid",
                "payload_sha256": "0" * 64,
            }
        ),
        encoding="utf-8",
    )
    return descriptor


def _jdf_fetcher(destination: Path, _reporter: object) -> Path:
    destination.mkdir(parents=True)
    return destination


def _czptt_fetcher(_config: object, destination: Path, _reporter: object) -> Path:
    (destination / "sr70").mkdir(parents=True)
    (destination / "sr70" / "SR70.csv").write_text("570760,Praha,50.083,14.435\n", "utf-8")
    return destination


def _build(options: cli.BuildOptions, compiler: _Compiler, **overrides: Any) -> cli.Release:
    arguments: dict[str, Any] = {
        "downloader": _downloader,
        "command_runner": compiler,
        "jdf_source_fetcher": _jdf_fetcher,
        "czptt_source_fetcher": _czptt_fetcher,
        **overrides,
    }
    return cli.build(replace(options, progress="off"), **arguments)


@pytest.fixture(autouse=True)
def _preflight(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    osm = tmp_path / "work" / "osm"

    def valid_snapshot(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {"merge_key": "fixture-osm"}

    def derive_extracts(_osm_file: Path, _workdir: Path, merge_key: str) -> tuple[Path, ...]:
        assert merge_key == "fixture-osm"
        return (
            osm / "railway-locations.osm.pbf",
            osm / "jdf-post-candidates.osm.pbf",
            osm / "jdf-transit-routing-base.osm.pbf",
        )

    monkeypatch.setattr(cli.osm_snapshot, "validate_snapshot", valid_snapshot)
    monkeypatch.setattr(cli.osm_snapshot, "derive_extracts", derive_extracts)
    monkeypatch.setattr(cli.national_jdf, "geodata_manifest", valid_snapshot)

    def git_identity(_path: Path) -> dict[str, object]:
        return {"commit": "abc", "dirty": False}

    monkeypatch.setattr(cli.jrutil, "git_identity", git_identity)
    monkeypatch.setattr(gvd, "prague_today", lambda: REFERENCE_DATE)


def test_build_compiles_one_specification_and_publishes_the_release(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    compiler = _Compiler()

    result = _build(cli.BuildOptions(runtime, 2027, jobs=4, estimated_posts=True), compiler)

    run = tmp_path / "work" / "runs" / "production" / result.run_id
    sources = run / "sources"
    partial = runtime.artifact_root / "releases" / f".{result.run_id}.part"
    osm = tmp_path / "work" / "osm"
    geodata = runtime.jrunify_ext_geodata_dir
    assert compiler.spec_text == (
        f"""[czptt]
snapshot = "{_posix(sources / "czptt")}"
osm = "{_posix(osm / "railway-locations.osm.pbf")}"
timetable_year = 2027
output = "{_posix(partial / "czptt")}"

[[czptt.train_keys]]
profile = "ids-jmk"
feed = "{_posix(sources / "regional" / "ids-jmk-gtfs.zip")}"

[jdf]
catalogues = "{_posix(geodata / "other")}"
gazetteer = "{_posix(runtime.jrunify_ext_geodata_dir / "gazetteer")}"
osm = "{_posix(osm / "jdf-post-candidates.osm.pbf")}"
route_rules = "{_posix(geodata / "routes")}"
timetable_year = 2027
reference_date = "2026-12-01"
output = "{_posix(partial / "jdf")}"

[[jdf.exports]]
name = "vld"
archive = "{_posix(sources / "national-jdf" / "JDF_VLD.zip")}"

[[jdf.exports]]
name = "drahy"
archive = "{_posix(sources / "national-jdf" / "JDF_drahy.zip")}"

[jdf.posts]
routing = "{_posix(osm / "jdf-transit-routing-base.osm.pbf")}"
policy = "{_posix(national_jdf.DEFAULT_POST_INFERENCE_POLICY)}"
reports = "{_posix(run / "diagnostics" / "posts")}"
cache = "{_posix(runtime.routing_cache_dir)}"

[jdf.overlay]
reports = "{_posix(run / "diagnostics" / "regional-overlay")}"

[[jdf.overlay.sources]]
profile = "pid"
feed = "{_posix(sources / "regional" / "pid-gtfs.zip")}"
overrides = "{_posix(geodata / "overlay" / "pid-stop-overrides.csv")}"

[[jdf.overlay.sources]]
profile = "ids-jmk"
feed = "{_posix(sources / "regional" / "ids-jmk-gtfs.zip")}"

[jdf.filtered]
rules = "{_posix(geodata / "filtered-jdf" / "rules-v1.json")}"
output = "{_posix(partial / "jdf-filtered")}"
"""
    )
    (cargo, cargo_cwd, _), (compile_command, _, env) = compiler.commands
    assert cargo == ["cargo", "build", "--release", "--locked", "-p", "jrutil"]
    assert cargo_cwd == runtime.jrutil.directory
    binary = cast(Path, runtime.jrutil.directory) / "target" / "release"
    assert Path(compile_command[0]).parent == binary
    assert compile_command[1:] == ["build", "--spec", str(run / "build.toml"), "--progress-events"]
    assert env == {"RAYON_NUM_THREADS": "4"}

    assert {path.name for path in result.root.iterdir()} == {
        "jdf",
        "czptt",
        "jdf-filtered",
        "release.json",
    }
    assert not sources.exists()
    current = json.loads((runtime.artifact_root / "current.json").read_text(encoding="utf-8"))
    assert current["run_id"] == result.run_id
    assert Path(current["jdf"]) == result.jdf
    assert result.jdf_filtered is not None
    assert Path(current["jdf_filtered"]) == result.jdf_filtered / "gtfs.zip"
    release = json.loads((result.root / "release.json").read_text(encoding="utf-8"))
    assert [stage["name"] for stage in release["stages"]] == [
        "fetch-sources",
        "build-jrutil",
        "route-shapes-graph",
        "compile",
        "validation",
    ]
    assert release["compiler"]["git"]["commit"] == "abc"
    assert "policy" not in release
    assert set(release["packages"]) == {"jdf", "czptt"}
    assert release["packages"]["jdf"]["compiler"]["tool"] == "jrutil-rs"
    assert set(release["outputs"]["jdf_filtered"]) == {"gtfs_sha256", "filter_report_sha256"}
    assert set(release["retrieval"]["seconds"]) == {"national-jdf", "national-czptt", "regional"}


def test_registry_shapes_and_disabled_options_shape_the_specification(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runtime = _runtime(tmp_path)
    registry = runtime.jrunify_ext_geodata_dir / "registry"
    registry.mkdir()
    (registry / "stops.csv").write_text("id\n", encoding="utf-8")
    shapes = route_shapes.RouteShapes(
        companion=tmp_path / "bin" / "obehy-motis-shapes",
        graph=tmp_path / "shapes" / "graph",
        cache=tmp_path / "shapes" / "results",
    )

    def prepare(**_kwargs: object) -> tuple[route_shapes.RouteShapes, None]:
        return shapes, None

    monkeypatch.setattr(cli.route_shapes, "prepare", prepare)
    compiler = _Compiler()

    result = _build(
        cli.BuildOptions(
            runtime,
            2027,
            keep_work=True,
            estimated_posts=True,
            filtered_jdf=False,
            routing_cache=False,
        ),
        compiler,
    )

    spec = compiler.spec
    jdf = cast(dict[str, Any], spec["jdf"])
    assert jdf["registry"] == _posix(registry)
    partial = runtime.artifact_root / "releases" / f".{result.run_id}.part"
    assert jdf["registry_candidates"] == _posix(partial / "stop-registry" / "stop-candidates.csv")
    assert "cache" not in jdf["posts"]
    assert "filtered" not in jdf
    expected_shapes = {
        "companion": _posix(shapes.companion),
        "graph": _posix(shapes.graph),
        "cache": _posix(shapes.cache),
    }
    assert spec["czptt"]["shapes"] == expected_shapes
    assert jdf["shapes"] == expected_shapes
    assert compiler.commands[-1][2] is None
    assert {path.name for path in (result.root / "stop-registry").iterdir()} == {
        "stop-candidates.csv",
        "post-candidates.csv",
        "place-candidates.csv",
    }
    assert {path.name for path in result.root.iterdir()} == {
        "jdf",
        "czptt",
        "stop-registry",
        "release.json",
    }
    assert result.jdf_filtered is None
    current = json.loads((runtime.artifact_root / "current.json").read_text(encoding="utf-8"))
    assert "jdf_filtered" not in current
    run = tmp_path / "work" / "runs" / "production" / result.run_id
    assert (run / "sources" / "czptt" / "sr70" / "SR70.csv").is_file()


def test_failure_preserves_previous_current(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.artifact_root.mkdir()
    previous = {"schema_version": 1, "run_id": "previous"}
    (runtime.artifact_root / "current.json").write_text(json.dumps(previous), encoding="utf-8")

    with pytest.raises(cli.ProductionPackageError):
        _build(cli.BuildOptions(runtime, 2027), _Compiler(publishable=False))

    assert json.loads((runtime.artifact_root / "current.json").read_text()) == previous
    failure_path = next((runtime.workdir / "runs" / "production").rglob("failure.json"))
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    assert [stage["name"] for stage in failure["completed_stages"]][-1] == "compile"


def test_existing_build_lock_rejects_concurrent_publication(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.artifact_root.mkdir()
    (runtime.artifact_root / ".production-build.lock").write_text("busy", encoding="utf-8")

    with pytest.raises(PipelineError, match="Another production build"):
        _build(cli.BuildOptions(runtime, 2027), _Compiler())

    assert not (runtime.artifact_root / "releases").exists()


def test_missing_gazetteer_fails_before_the_run_starts(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    (runtime.jrunify_ext_geodata_dir / "gazetteer" / "cz_towns.json.gz").unlink()

    with pytest.raises(PipelineError, match="cz_towns"):
        _build(cli.BuildOptions(runtime, 2027), _Compiler())

    assert not (runtime.workdir / "runs").exists()


def test_unreachable_source_fails_before_any_compilation(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    compiler = _Compiler()

    def unreachable(_url: str, source_id: str, _destination: Path, *, require_api: bool) -> Path:
        del require_api
        raise DownloadError(source_id, "https://example.invalid/gtfs.zip", 3, "handshake timed out")

    with pytest.raises(DownloadError, match="Download pid-gtfs failed after 3 attempts"):
        _build(cli.BuildOptions(runtime, 2027), compiler, downloader=unreachable)

    assert compiler.commands == []
    failure_path = next((runtime.workdir / "runs" / "production").rglob("failure.json"))
    failure = json.loads(failure_path.read_text(encoding="utf-8"))
    assert failure["completed_stages"] == []
    assert "pid-gtfs" in failure["error"]
    assert (failure_path.parent / "sources" / "fetch-log.json").is_file()


def test_build_uses_the_given_run_id(tmp_path: Path) -> None:
    run_id = "20261124T070233Z-1bda58bef584"
    options = cli.BuildOptions(_runtime(tmp_path), 2027, estimated_posts=True, run_id=run_id)

    result = _build(options, _Compiler())

    assert result.run_id == run_id
    assert (tmp_path / "work" / "runs" / "production" / run_id).is_dir()


def test_malformed_run_id_is_rejected() -> None:
    with pytest.raises(SystemExit):
        cli.main(["build", "--run-id", "build-1"])


def test_removed_build_options_are_rejected() -> None:
    for option in ("--memory-budget=8GiB", "--czptt-operational-points=gtfs"):
        with pytest.raises(SystemExit):
            cli.main(["build", option])


def test_api_openapi_writes_the_document_without_a_database(tmp_path: Path) -> None:
    out = tmp_path / "openapi.json"
    assert cli.main(["api", "openapi", "--out", str(out)]) == 0
    document = json.loads(out.read_text(encoding="utf-8"))
    assert "/api/v1/stop-places/{location_id}/departures" in document["paths"]
    assert "Departure" in document["components"]["schemas"]
