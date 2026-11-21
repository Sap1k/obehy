"""`obehy jobs nightly` against PostgreSQL: retention by the policy values of `realtime_config`,
without the Python worker (these jobs stay Python after the switch)."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import psycopg
import pytest

from obehy.realtime.jobs import nightly
from obehy.realtime_config import policy_values

pytestmark = pytest.mark.postgres


def test_nightly_prunes_each_sources_raw_days_by_its_window(
    connection: psycopg.Connection, tmp_path: Path
) -> None:
    archive = tmp_path / "rt-raw"
    for source, channel in (("pid", "vehicles"), ("sz-tabule", "boards")):
        for day in ("2026-11-01", "2026-11-19", "2026-11-20", "2026-11-21"):
            (archive / source / channel / day).mkdir(parents=True)

    lines = list(
        nightly(connection, archive, policy_values(), datetime(2026, 11, 21, 3, tzinfo=UTC))
    )

    assert lines[0] == "dropped 0 history days before 2026-10-31"
    assert lines[1].startswith("deleted 0 alerts withdrawn before 2026-11-07")
    assert lines[2].startswith("deleted 0 lookup requests before 2026-11-07")
    assert lines[3:] == [
        "deleted 2 raw archive days of pid before 2026-11-20",
        "deleted 1 raw archive days of sz-tabule before 2026-11-07",
    ]
    assert sorted(p.name for p in (archive / "pid" / "vehicles").iterdir()) == [
        "2026-11-20",
        "2026-11-21",
    ]
