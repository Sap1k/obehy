from __future__ import annotations

import os
from pathlib import Path

import pytest

from obehy.national_jdf import BuildConfig, build

WORKSPACE = Path(__file__).parents[3]

pytestmark = [
    pytest.mark.national_jdf_live,
    pytest.mark.skipif(
        os.environ.get("OBEHY_RUN_NATIONAL_JDF_SMOKE") != "1",
        reason="set OBEHY_RUN_NATIONAL_JDF_SMOKE=1 for the large live download",
    ),
]


def test_live_national_jdf_build(tmp_path: Path) -> None:
    output = tmp_path / "national-jdf"

    build(
        BuildConfig(
            output=output,
            workdir=WORKSPACE / "work",
            osm_file=WORKSPACE / "obehy-region.osm.pbf",
            jrutil_root=WORKSPACE / "jrutil",
            jrutil_command=None,
            geodata_root=WORKSPACE / "jrunify-ext-geodata" / "other",
        )
    )

    assert (output / "bundle" / "manifest.json").is_file()
    assert (output / "run-manifest.json").is_file()
