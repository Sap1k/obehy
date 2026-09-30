# pyright: reportUnusedFunction=false
from __future__ import annotations

import json
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from obehy import cli
from obehy.national_jdf import PipelineError
from obehy.runtime_config import JrUtilRuntime, RuntimeConfig


def _runtime(tmp_path: Path) -> RuntimeConfig:
    geodata = tmp_path / "geodata" / "other"
    geodata.mkdir(parents=True)
    (geodata / "municipalities.csv").write_text("Town,Stop,49,14,CZ\n", encoding="utf-8")
    jrutil = tmp_path / "jrutil"
    project = jrutil / "jrutil-multitool" / "jrutil-multitool.fsproj"
    project.parent.mkdir(parents=True)
    project.write_text("<Project />\n", encoding="utf-8")
    return RuntimeConfig(
        source=tmp_path / "obehy.toml",
        workdir=tmp_path / "work",
        artifact_root=tmp_path / "artifacts",
        osm_file=tmp_path / "osm.pbf",
        jrunify_ext_geodata_dir=tmp_path / "geodata",
        jrutil=JrUtilRuntime(directory=jrutil, command=None),
    )


def _package(path: Path, *, publishable: bool = True) -> None:
    path.mkdir(parents=True)
    with zipfile.ZipFile(path / "gtfs.zip", "w") as archive:
        archive.writestr("agency.txt", "agency_id,agency_name\na,Agency\n")
    (path / "serving").mkdir()
    (path / "diagnostics.json").write_text("{}\n", encoding="utf-8")
    (path / "manifest.json").write_text(
        json.dumps(
            {
                "bundle_format": "jrutil-production",
                "bundle_version": 2,
                "serving_schema_version": 3,
                "contract_valid": True,
                "publication_eligible": publishable,
                "feed_version": "fixture",
                "compiler": {"tool": "jrutil", "version": "fixture-version"},
            }
        ),
        encoding="utf-8",
    )


def _dependencies(
    runtime: RuntimeConfig, *, overlay_publishable: bool = True, fail_czptt: bool = False
) -> tuple[Any, Any, Any, Any, Any, list[Any]]:
    seen: list[Any] = []

    def jdf_builder(config: Any, **_kwargs: object) -> Path:
        seen.append(config)
        output = config.output
        _package(output / "bundle")
        return output

    def czptt_builder(config: Any, **_kwargs: object) -> Path:
        seen.append(config)
        if fail_czptt:
            raise PipelineError("fixture CZPTT failure")
        output = config.output
        _package(output / "bundle")
        return output

    def downloader(_url: str, source_id: str, destination: Path, *, require_api: bool) -> Path:
        seen.append((source_id, require_api))
        destination.write_bytes(b"fixture")
        descriptor = destination.with_name(f"{source_id}-descriptor.json")
        descriptor.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_id": source_id,
                    "retrieved_at": "2026-09-14T12:00:00+00:00",
                    "source_uri": "https://example.invalid",
                    "payload_sha256": "0" * 64,
                }
            ),
            encoding="utf-8",
        )
        return descriptor

    def runner(
        command: Sequence[str],
        _cwd: Path,
        _log: Path,
        _reporter: object,
        _progress: object,
    ) -> None:
        seen.append(list(command))
        if "regional-gtfs-overlay" in command:
            _package(Path(command[-1]), publishable=overlay_publishable)

    def filtered_builder(bundle: Path, destination: Path, **kwargs: object) -> Path:
        seen.append(("filtered-jdf", bundle, kwargs))
        destination.mkdir(parents=True)
        (destination / "gtfs.zip").write_bytes(b"fixture")
        (destination / "filter-report.json").write_text("{}\n", encoding="utf-8")
        return destination

    return jdf_builder, czptt_builder, downloader, filtered_builder, runner, seen


@pytest.fixture(autouse=True)
def _preflight(monkeypatch: pytest.MonkeyPatch) -> None:
    def valid_snapshot(*_args: object, **_kwargs: object) -> dict[str, Any]:
        return {}

    monkeypatch.setattr(cli.osm_snapshot, "validate_snapshot", valid_snapshot)
    monkeypatch.setattr(cli.national_jdf, "geodata_manifest", valid_snapshot)


def test_build_publishes_exact_pair_and_switches_current(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    jdf, czptt, downloader, filtered, runner, seen = _dependencies(runtime)

    result = cli.build(
        cli.BuildOptions(runtime, 2027, estimated_posts=True, progress="off"),
        jdf_builder=jdf,
        czptt_builder=czptt,
        downloader=downloader,
        filtered_jdf_builder=filtered,
        command_runner=runner,
    )

    assert {path.name for path in result.root.iterdir()} == {
        "jdf",
        "czptt",
        "jdf-filtered",
        "release.json",
    }
    current = json.loads((runtime.artifact_root / "current.json").read_text(encoding="utf-8"))
    assert current["run_id"] == result.run_id
    assert Path(current["jdf"]) == result.jdf
    assert Path(current["czptt"]) == result.czptt
    assert result.jdf_filtered is not None
    assert Path(current["jdf_filtered"]) == result.jdf_filtered / "gtfs.zip"
    filtered_calls = [
        cast(tuple[str, Path, dict[str, object]], value)
        for value in seen
        if isinstance(value, tuple) and value[0] == "filtered-jdf"
    ]
    assert len(filtered_calls) == 1
    assert (
        filtered_calls[0][1]
        == tmp_path / "work" / "runs" / "production" / result.run_id / "national-jdf" / "bundle"
    )
    jdf_config = next(value for value in seen if isinstance(value, cli.national_jdf.BuildConfig))
    czptt_config = next(
        value for value in seen if isinstance(value, cli.national_czptt.BuildConfig)
    )
    assert jdf_config.estimated_posts is True
    assert jdf_config.build_jrutil is False
    assert czptt_config.geodata_root == runtime.jrunify_ext_geodata_dir
    assert czptt_config.timetable_year == 2027
    assert czptt_config.build_jrutil is False
    assert czptt_config.memory_budget == "auto"
    assert czptt_config.operational_points == "sidecar"
    build_count = 0
    for value in seen:
        if isinstance(value, list) and "build" in value:
            build_count += 1
    assert build_count == 1
    overlay: list[str] | None = None
    for value in seen:
        if isinstance(value, list) and "regional-gtfs-overlay" in value:
            overlay = cast(list[str], value)
            break
    assert overlay is not None
    assert sum(item.startswith("--source=") for item in overlay) == 2
    assert "--memory-budget=auto" in overlay
    assert "--converter-version=fixture-version" in overlay


@pytest.mark.parametrize("overlay_publishable,fail_czptt", [(False, False), (True, True)])
def test_failure_preserves_previous_current(
    tmp_path: Path, overlay_publishable: bool, fail_czptt: bool
) -> None:
    runtime = _runtime(tmp_path)
    runtime.artifact_root.mkdir()
    previous = {"schema_version": 1, "run_id": "previous"}
    (runtime.artifact_root / "current.json").write_text(json.dumps(previous), encoding="utf-8")
    jdf, czptt, downloader, filtered, runner, _seen = _dependencies(
        runtime, overlay_publishable=overlay_publishable, fail_czptt=fail_czptt
    )

    with pytest.raises((PipelineError, cli.ProductionPackageError)):
        cli.build(
            cli.BuildOptions(runtime, 2027, progress="off"),
            jdf_builder=jdf,
            czptt_builder=czptt,
            downloader=downloader,
            filtered_jdf_builder=filtered,
            command_runner=runner,
        )

    assert json.loads((runtime.artifact_root / "current.json").read_text()) == previous
    assert list((runtime.workdir / "runs" / "production").rglob("failure.json"))


def test_existing_build_lock_rejects_concurrent_publication(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    runtime.artifact_root.mkdir()
    (runtime.artifact_root / ".production-build.lock").write_text("busy", encoding="utf-8")
    jdf, czptt, downloader, filtered, runner, _seen = _dependencies(runtime)

    with pytest.raises(PipelineError, match="Another production build"):
        cli.build(
            cli.BuildOptions(runtime, 2027, progress="off"),
            jdf_builder=jdf,
            czptt_builder=czptt,
            downloader=downloader,
            filtered_jdf_builder=filtered,
            command_runner=runner,
        )

    assert not (runtime.artifact_root / "releases").exists()


def test_production_overlay_policies_enable_all_nonblocking_coverage_gates() -> None:
    policy_root = cli.POLICY.parent
    expected = {"route", "stop", "trip", "trip_date", "call", "call_date", "shape", "transfer"}
    for name in (
        "pid-production-v1.json",
        "ids-jmk-production-v1.json",
        "pid-ids-jmk-production-v1.json",
    ):
        policy = json.loads((policy_root / name).read_text(encoding="utf-8"))
        assert policy["calibration"] is False
        assert policy["publication_enabled"] is True
        assert policy["minimum_coverage"] == dict.fromkeys(expected, 0)

    combined = json.loads(cli.POLICY.read_text(encoding="utf-8"))
    assert {source["source_id"] for source in combined["sources"]} == {
        "pid-gtfs",
        "ids-jmk-gtfs",
    }


def test_filtered_jdf_can_be_skipped(tmp_path: Path) -> None:
    runtime = _runtime(tmp_path)
    jdf, czptt, downloader, filtered, runner, seen = _dependencies(runtime)

    result = cli.build(
        cli.BuildOptions(
            runtime, 2027, progress="off", filtered_jdf=False, czptt_operational_points="gtfs"
        ),
        jdf_builder=jdf,
        czptt_builder=czptt,
        downloader=downloader,
        filtered_jdf_builder=filtered,
        command_runner=runner,
    )

    assert result.jdf_filtered is None
    assert not (result.root / "jdf-filtered").exists()
    current = json.loads((runtime.artifact_root / "current.json").read_text(encoding="utf-8"))
    assert "jdf_filtered" not in current
    czptt_config = next(
        value for value in seen if isinstance(value, cli.national_czptt.BuildConfig)
    )
    assert czptt_config.operational_points == "gtfs"
