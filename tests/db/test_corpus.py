"""Golden replays of pinned corpora (docs/R1_SLICE.md sections 6 and 7).

Opt-in and slow (minutes): set `OBEHY_PINNED_ROOT` to the pinned corpora directory. The replay
runs against the development database named by the local config, where the corpus's release
must be loaded. A difference is either a bug or an intended change; regenerate the digests with
`OBEHY_UPDATE_GOLDEN=1` only for the latter.
"""

from __future__ import annotations

import hashlib
import json
import os
from datetime import date
from pathlib import Path
from typing import Any

import psycopg
import pytest

from obehy.realtime.policy import load_policy
from obehy.realtime.replay import ReplayOptions, replay
from obehy.runtime_config import load_database_url

GOLDEN = Path(__file__).resolve().parents[1] / "golden"
PINNED = os.environ.get("OBEHY_PINNED_ROOT")

pytestmark = pytest.mark.skipif(not PINNED, reason="OBEHY_PINNED_ROOT is not set")


def _quiet(_: str) -> None:
    pass


@pytest.mark.parametrize("golden", sorted(GOLDEN.glob("*.json")), ids=lambda p: p.stem)
def test_pinned_corpus_replays_to_its_golden_outputs(golden: Path, tmp_path: Path) -> None:
    expected: dict[str, Any] = json.loads(golden.read_text(encoding="utf-8"))
    corpus = Path(str(PINNED)) / expected["corpus"]
    if not corpus.is_dir():
        pytest.skip(f"corpus {expected['corpus']} is not pinned here")
    options = expected["options"]
    out = tmp_path / "out"
    with psycopg.connect(load_database_url(), autocommit=True) as connection:
        replay(
            connection,
            ReplayOptions(
                release=expected["release"],
                archive=corpus / "archive",
                start=date.fromisoformat(options["from"]),
                end=date.fromisoformat(options["to"]),
                channels=[(source, channel) for source, channel in options["channels"]],
                out=out,
                feeds=tuple(options["feeds"]),
                gtfs_rt_every_s=options["gtfs_rt_every_s"],
            ),
            load_policy(),
            report=_quiet,
        )
    actual = {
        p.relative_to(out).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(out.rglob("*"))
        if p.is_file()
    }
    if os.environ.get("OBEHY_UPDATE_GOLDEN"):
        expected["outputs"] = actual
        golden.write_text(json.dumps(expected, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    changed = sorted(
        k
        for k in actual.keys() | expected["outputs"].keys()
        if actual.get(k) != expected["outputs"].get(k)
    )
    assert not changed, f"{len(changed)} outputs differ, first: {changed[:5]}"
