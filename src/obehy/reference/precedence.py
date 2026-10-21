"""Which statement about a vehicle's attribute counts, and the vehicle's profile
(BASE_PLAN.md section 27). Pure.

The registry's datasets (curated, the lower priority first) beat what sources state live,
which beats a model's default. Among live statements the vocabulary ranks the sources (per
attribute, else `live_sources`), the most recent first among equals. A winner that another
curated or live statement contradicts is flagged; every statement is kept.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Literal

from obehy.reference.registry import CuratedValue, Entry
from obehy.reference.vocabulary import Vocabulary

Origin = Literal["curated", "live", "model"]
_ORIGIN: dict[str, int] = {"curated": 0, "live": 1, "model": 2}
# The attributes a profile also carries as columns, for boards and lists.
FLAGS = ("low_floor", "air_conditioning", "wheelchair_accessible")
TEXTS = ("manufacturer", "model", "operator", "licence_plate", "fleet_number")


@dataclass(frozen=True, slots=True)
class Statement:
    attribute: str
    value: str
    origin: Origin
    source: str  # the dataset (curated, model) or the realtime source (live)
    priority: int | None = None  # a dataset's
    observed_at: datetime | None = None  # a live value's last_seen

    @classmethod
    def curated(cls, value: CuratedValue) -> Statement:
        return cls(value.attribute, value.value, value.origin, value.dataset, value.priority)


@dataclass(frozen=True, slots=True)
class Picked:
    winner: Statement
    conflict: bool
    statements: tuple[Statement, ...]


def _rank(s: Statement, vocabulary: Vocabulary) -> tuple[int, int, float, str, str]:
    if s.origin == "live":
        within = vocabulary.source_rank(s.attribute, s.source)
    else:
        within = s.priority if s.priority is not None else 1 << 20
    recency = -s.observed_at.timestamp() if s.observed_at is not None else 0.0
    return (_ORIGIN[s.origin], within, recency, s.source, s.value)


def pick(statements: Iterable[Statement], vocabulary: Vocabulary) -> dict[str, Picked]:
    """Per attribute, the statement that counts."""

    grouped: defaultdict[str, list[Statement]] = defaultdict(list)
    for s in statements:
        grouped[s.attribute].append(s)
    picked: dict[str, Picked] = {}
    for attribute in sorted(grouped):
        ordered = tuple(sorted(grouped[attribute], key=lambda s: _rank(s, vocabulary)))
        winner = ordered[0]
        conflict = any(s.value != winner.value for s in ordered if s.origin in ("curated", "live"))
        picked[attribute] = Picked(winner, conflict, ordered)
    return picked


@dataclass(frozen=True, slots=True)
class Profile:
    """What is known about one vehicle, as `rt.vehicle_profile` stores it."""

    vehicle_id: str
    registered: bool
    public_label: str | None
    flags: dict[str, bool | None]
    texts: dict[str, str | None]
    datasets: tuple[str, ...]
    sources: tuple[str, ...]
    attributes: tuple[dict[str, Any], ...]
    last_stated: datetime | None  # the latest live statement


def _iso(value: datetime | None) -> str | None:
    return None if value is None else value.isoformat()


def _statement_json(s: Statement) -> dict[str, Any]:
    return {
        "value": s.value,
        "origin": s.origin,
        "source": s.source,
        "observed_at": _iso(s.observed_at),
    }


def profile(
    vehicle_id: str,
    entry: Entry | None,
    curated: Iterable[CuratedValue],
    live: Iterable[Statement],
    vocabulary: Vocabulary,
) -> Profile:
    live = tuple(live)
    picked = pick([*(Statement.curated(c) for c in curated), *live], vocabulary)
    return Profile(
        vehicle_id=vehicle_id,
        registered=entry is not None,
        public_label=None if entry is None else entry.public_label,
        flags={a: None if a not in picked else picked[a].winner.value == "true" for a in FLAGS},
        texts={a: None if a not in picked else picked[a].winner.value for a in TEXTS},
        datasets=() if entry is None else entry.datasets,
        sources=tuple(sorted({s.source for s in live})),
        attributes=tuple(
            {
                **_statement_json(p.winner),
                "attribute": attribute,
                "conflict": p.conflict,
                "statements": [_statement_json(s) for s in p.statements],
            }
            for attribute, p in picked.items()
        ),
        last_stated=max((s.observed_at for s in live if s.observed_at), default=None),
    )
