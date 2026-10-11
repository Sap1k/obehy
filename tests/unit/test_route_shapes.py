"""Route-shapes preparation for the static build (BASE_PLAN.md section 17)."""

from __future__ import annotations

import json
from collections.abc import Sequence
from pathlib import Path

from obehy import national_czptt, regional_overlay, route_shapes
from obehy.pipeline.reporting import BuildReporter, CommandProgress, Reporter

SHAPES = route_shapes.RouteShapes(
    companion=Path("/c/obehy-motis-shapes"),
    graph=Path("/g"),
    cache=Path("/cache"),
)


class _Extract:
    """A fake `obehy-motis-shapes extract` that writes the graph key file."""

    def __init__(self, commit: str) -> None:
        self.calls = 0
        self.commit = commit

    def __call__(
        self,
        command: Sequence[str],
        cwd: Path,
        log: Path,
        reporter: Reporter | None,
        progress: CommandProgress | None,
    ) -> None:
        self.calls += 1
        args = list(command)
        graph = Path(args[args.index("--graph") + 1])
        graph.mkdir(parents=True)
        key = args[args.index("--key") + 1]
        (graph / route_shapes.GRAPH_KEY_FILE).write_text(f"{key}\n{self.commit}\n", "utf-8")


def _prepare(tmp_path: Path, extract: _Extract, companion: Path | None, commit: str):
    def route_filter(source: Path, destination: Path, **_: object) -> Path:
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(b"pbf")
        return destination

    return route_shapes.prepare(
        companion=companion,
        osm_file=tmp_path / "snapshot.osm.pbf",
        osm_key=lambda: "merge-key",
        run_dir=tmp_path,
        cache=tmp_path / "cache",
        log=tmp_path / "graph.log",
        reporter=BuildReporter("off"),
        command_runner=extract,
        route_filter=route_filter,
        osr_commit=lambda _: commit,
    )


def test_without_a_companion_the_phase_is_skipped_untouched(tmp_path: Path) -> None:
    extract = _Extract("abc")
    shapes, reason = _prepare(tmp_path, extract, None, "abc")
    assert shapes is None and reason is not None and "route_shapes_companion" in reason
    shapes, reason = _prepare(tmp_path, extract, tmp_path / "missing", "abc")
    assert shapes is None and reason is not None and "not found" in reason
    assert extract.calls == 0


def test_the_graph_is_extracted_once_per_pbf_and_osr(tmp_path: Path) -> None:
    companion = tmp_path / "obehy-motis-shapes"
    companion.write_bytes(b"")
    extract = _Extract("abc")
    shapes, reason = _prepare(tmp_path, extract, companion, "abc")
    assert reason is None and shapes is not None
    assert shapes.arguments() == [
        f"--route-shapes={companion}",
        f"--route-shapes-graph={shapes.graph}",
        f"--route-shapes-cache={tmp_path / 'cache' / 'results'}",
    ]
    _prepare(tmp_path, extract, companion, "abc")
    assert extract.calls == 1
    # Another osr build cannot read the graph: extract again.
    extract.commit = "def"
    _prepare(tmp_path, extract, companion, "def")
    assert extract.calls == 2


def test_both_final_packages_get_the_options(tmp_path: Path) -> None:
    config = national_czptt.BuildConfig(
        output=tmp_path / "out",
        workdir=tmp_path,
        osm_file=tmp_path / "osm.pbf",
        geodata_root=tmp_path,
        jrutil_root=None,
        jrutil_command=("jrutil",),
        route_shapes=tuple(SHAPES.arguments()),
    )
    command = national_czptt._converter_command(  # pyright: ignore[reportPrivateUsage]
        config, tmp_path / "derived" / "messages.zip", tmp_path / "catalog.json", tmp_path / "b"
    )
    assert command[-2:] == [str(tmp_path / "derived" / "messages.zip"), str(tmp_path / "b")]
    assert command[-5:-2] == SHAPES.arguments()

    commands: list[list[str]] = []

    def record(command: Sequence[str], *_: object) -> None:
        commands.append(list(command))

    (tmp_path / "base").mkdir()
    (tmp_path / "base" / "manifest.json").write_text(
        json.dumps({"compiler": {"version": "test"}}), encoding="utf-8"
    )
    regional_overlay.run(
        runtime_command=["jrutil"],
        cwd=tmp_path,
        base=tmp_path / "base",
        output=tmp_path / "jdf",
        sources=[],
        gvd_year=2026,
        jobs=1,
        memory_budget="auto",
        diagnostics=tmp_path / "diagnostics",
        log=tmp_path / "log",
        reporter=BuildReporter("off"),
        command_runner=record,
        route_shapes=SHAPES.arguments(),
    )
    assert commands[0][-6:-3] == SHAPES.arguments()
    assert commands[0][-2:] == [str(tmp_path / "base"), str(tmp_path / "jdf")]


def test_summaries_copy_each_package_section(tmp_path: Path) -> None:
    for name, section in (("jdf", {"shaped_trips": 3}), ("czptt", None)):
        (tmp_path / name).mkdir()
        diagnostics: dict[str, object] = {"schema_version": 1}
        if section is not None:
            diagnostics["route_shapes"] = section
        (tmp_path / name / "diagnostics.json").write_text(json.dumps(diagnostics), "utf-8")
    assert route_shapes.summaries({"jdf": tmp_path / "jdf", "czptt": tmp_path / "czptt"}) == {
        "jdf": {"shaped_trips": 3}
    }
