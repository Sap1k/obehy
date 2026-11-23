"""Route-shapes preparation for the static build (BASE_PLAN.md section 17)."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path

from obehy import route_shapes
from obehy.pipeline.reporting import BuildReporter, CommandProgress, Reporter


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
        env: Mapping[str, str] | None = None,
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
    assert shapes == route_shapes.RouteShapes(
        companion=companion,
        graph=(tmp_path / "cache" / "graph").resolve(),
        cache=(tmp_path / "cache" / "results").resolve(),
    )
    _prepare(tmp_path, extract, companion, "abc")
    assert extract.calls == 1
    # Another osr build cannot read the graph: extract again.
    extract.commit = "def"
    _prepare(tmp_path, extract, companion, "def")
    assert extract.calls == 2


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
