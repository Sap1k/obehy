"""Station pairing (BASE_PLAN.md section 7.4): which surface stop each railway station of a
load pair shares its search result and board with, decided once at activation into
`control.station_pairing` (`sql/station_pairing.sql`) and read through `active.stop_group`.
Presentation only: no ID is merged. Ties are quarantined (`ambiguous`) for the curated
overrides (`ref.station_pair`, `obehy ref import station-pairs`) to settle."""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, LiteralString, cast

import psycopg

POLICY = Path(__file__).resolve().parents[1] / "data" / "stops" / "station-pairing-v1.toml"
_SQL = Path(__file__).resolve().parent / "sql" / "station_pairing.sql"
# The overrides' dataset in ref.dataset.
OVERRIDES = "station-pairs"


@dataclass(frozen=True, slots=True)
class PairingPolicy:
    version: str
    radius_m: int
    candidates: int
    margin_m: int
    station_words: tuple[str, ...]


def station_word(text: str) -> str:
    """A station word as the pairing matches it: lower case letters and digits, any other run
    of characters one space (the words are written without diacritics)."""

    return " ".join(re.findall(r"[a-z0-9]+", text.lower()))


def load_pairing_policy(path: Path = POLICY) -> PairingPolicy:
    with path.open("rb") as stream:
        document = tomllib.load(stream)
    return PairingPolicy(
        version=str(document["version"]),
        radius_m=int(document["radius_m"]),
        candidates=int(document["candidates"]),
        margin_m=int(document["margin_m"]),
        station_words=tuple(station_word(w) for w in document["station_words"]),
    )


def overrides_version(connection: psycopg.Connection) -> str:
    row = connection.execute(
        "SELECT version FROM ref.dataset WHERE dataset = %s", (OVERRIDES,)
    ).fetchone()
    return cast(str, row[0]) if row else ""


def pair(
    connection: psycopg.Connection,
    load_ids: tuple[int, int],
    policy: PairingPolicy | None = None,
    *,
    force: bool = False,
) -> bool:
    """Pair the stations of `load_ids` (jdf, czptt) unless they were paired by this policy and
    these overrides already (or `force`); True when it paired. Runs in the caller's
    transaction, or its own."""

    policy = policy or load_pairing_policy()
    jdf, czptt = load_ids
    with connection.transaction():
        overrides = overrides_version(connection)
        current = connection.execute(
            "SELECT policy_version, overrides_version FROM control.station_pairing_run"
            " WHERE jdf_load_id = %s AND czptt_load_id = %s FOR UPDATE",
            load_ids,
        ).fetchone()
        if not force and current == (policy.version, overrides):
            return False
        connection.execute(
            "DELETE FROM control.station_pairing_run WHERE jdf_load_id = %s AND czptt_load_id = %s",
            load_ids,
        )
        connection.execute(
            "INSERT INTO control.station_pairing_run"
            " (jdf_load_id, czptt_load_id, policy_version, overrides_version)"
            " VALUES (%s, %s, %s, %s)",
            (jdf, czptt, policy.version, overrides),
        )
        params: dict[str, Any] = {
            "jdf": jdf,
            "czptt": czptt,
            "radius": policy.radius_m,
            # A box safely larger than the radius at Czech latitudes (cos 51° ≈ 0.63).
            "degrees": policy.radius_m / 111_320 / 0.6,
            "candidates": policy.candidates,
            "margin": policy.margin_m,
            "station_words": list(policy.station_words),
        }
        connection.execute(cast(LiteralString, _SQL.read_text(encoding="utf-8")), params)
    return True


def summary(connection: psycopg.Connection, load_ids: tuple[int, int]) -> dict[str, Any]:
    """The pairing of `load_ids`: what decided it, whether that is still current, and how many
    stations came out each way."""

    run = connection.execute(
        "SELECT policy_version, overrides_version, computed_at FROM control.station_pairing_run"
        " WHERE jdf_load_id = %s AND czptt_load_id = %s",
        load_ids,
    ).fetchone()
    if run is None:
        return {"paired_at": None, "stale": True}
    counts = dict(
        connection.execute(
            "SELECT outcome || '/' || reason, count(*) FROM control.station_pairing"
            " WHERE jdf_load_id = %s AND czptt_load_id = %s GROUP BY 1 ORDER BY 1",
            load_ids,
        ).fetchall()
    )
    stale = (run[0], run[1]) != (load_pairing_policy().version, overrides_version(connection))
    return {
        "paired_at": run[2],
        "policy_version": run[0],
        "overrides_version": run[1] or None,
        "stale": stale,
        "outcomes": counts,
    }
