"""Staging and run directories of a build and their atomic activation."""

from __future__ import annotations

import os
import shutil
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

from obehy.pipeline.errors import PipelineError
from obehy.pipeline.reporting import Reporter


@dataclass(frozen=True)
class Staging:
    """`publish` is renamed onto `output` on success; `run_root` holds scratch work.

    A failed build leaves both in place, with `failure.json` at the staging root.
    """

    output: Path
    stage: Path
    publish: Path
    run_root: Path
    work: Path

    @property
    def failure_path(self) -> Path:
        return self.stage / "failure.json"

    def activate(self, reporter: Reporter, *, keep_work: bool) -> Path:
        if keep_work:
            os.replace(self.work, self.publish / "work")
        os.replace(self.publish, self.output)
        try:
            if self.stage.exists():
                shutil.rmtree(self.stage)
            if not keep_work and self.run_root.exists():
                shutil.rmtree(self.run_root)
        except OSError as error:
            reporter.problem(
                "warning",
                f"Published output successfully but could not remove scratch directory "
                f"{self.stage}: {error}",
            )
            reporter.note(f"SCRATCH CLEANUP RETAINED: {self.stage}")
        return self.output


def create(output: Path, workdir: Path, pipeline: str) -> Staging:
    """Create a sibling staging directory for `output` and a run directory under `workdir`."""

    output = output.resolve()
    if output.exists():
        raise PipelineError(f"Output path must not exist: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    stage = output.parent / f".{output.name}.work-{uuid.uuid4().hex}"
    run_root = (
        workdir.resolve()
        / "runs"
        / pipeline
        / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}-{uuid.uuid4().hex}"
    )
    staging = Staging(output, stage, stage / "publish", run_root, run_root / "work")
    for directory in (staging.publish, staging.work):
        directory.mkdir(parents=True)
    return staging
