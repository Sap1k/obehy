"""Route shapes for the static build (BASE_PLAN.md section 17, STATIC_PIPELINE.md "Route shapes").

Oběhy prepares what the route-shapes phase of JrUtil's package writer needs: the routing PBF
(filtered from the OSM snapshot) and the companion's osr graph. The phase itself runs inside
`regional-gtfs-overlay` and `czptt-to-bundle`, which get `arguments()`.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from obehy import osm_snapshot
from obehy.pipeline.files import file_digest
from obehy.pipeline.process import CommandFn
from obehy.pipeline.reporting import CommandProgress, Reporter

GRAPH_KEY_FILE = "obehy-graph.txt"


@dataclass(frozen=True)
class RouteShapes:
    companion: Path
    graph: Path
    cache: Path

    def arguments(self) -> list[str]:
        return [
            f"--route-shapes={self.companion}",
            f"--route-shapes-graph={self.graph}",
            f"--route-shapes-cache={self.cache}",
        ]


def companion_osr_commit(companion: Path) -> str:
    """The osr commit a companion binary was built with (`version` prints it last)."""

    output = subprocess.run(
        [str(companion), "version"], check=True, capture_output=True, text=True
    ).stdout.split()
    return output[-1]


def graph_matches(graph: Path, key: str, osr_commit: str) -> bool:
    """Whether `graph` was extracted from the PBF with `key` by the same osr."""

    path = graph / GRAPH_KEY_FILE
    if not path.is_file():
        return False
    return path.read_text(encoding="utf-8").splitlines() == [key, osr_commit]


FilterFn = Callable[..., Path]


def prepare(
    *,
    companion: Path | None,
    osm_file: Path,
    osm_key: Callable[[], str],
    run_dir: Path,
    cache: Path,
    log: Path,
    reporter: Reporter,
    command_runner: CommandFn,
    route_filter: FilterFn = osm_snapshot.filter_route_shapes,
    osr_commit: Callable[[Path], str] = companion_osr_commit,
) -> tuple[RouteShapes | None, str | None]:
    """The route-shapes arguments for JrUtil, or None and why the phase is skipped.

    `cache` holds the routing PBF and the graph, rebuilt only when the snapshot or the
    companion's osr changes, and the companion's results (`results/`).
    """

    if companion is None:
        return None, "no companion configured (paths.route_shapes_companion)"
    if not companion.is_file():
        return None, f"companion not found: {companion}"
    pbf = route_filter(osm_file, cache / "routing.osm.pbf", source_key=osm_key())
    key = f"sha256:{file_digest(pbf)}"
    commit = osr_commit(companion)
    graph = cache.resolve() / "graph"
    if not graph_matches(graph, key, commit):
        shutil.rmtree(graph, ignore_errors=True)
        command_runner(
            [str(companion), "extract", "--osm", str(pbf), "--graph", str(graph), "--key", key],
            run_dir,
            log,
            reporter,
            CommandProgress("Route-shapes graph", stage="route-shapes-graph"),
        )
    results = cache.resolve() / "results"
    results.mkdir(parents=True, exist_ok=True)
    return RouteShapes(companion=companion, graph=graph, cache=results), None


def summaries(packages: Mapping[str, Path]) -> dict[str, object]:
    """Each package's `route_shapes` diagnostics section, for release.json."""

    result: dict[str, object] = {}
    for name, package in packages.items():
        diagnostics = json.loads((package / "diagnostics.json").read_text(encoding="utf-8"))
        section = cast(dict[str, Any], diagnostics).get("route_shapes")
        if section is not None:
            result[name] = section
    return result
