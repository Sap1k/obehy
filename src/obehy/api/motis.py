"""MOTIS trip and stop IDs, and which of them name Oběhy trips (`journeys.py`).

MOTIS names a trip `{YYYYMMDD}_{HH:MM}_{tag}_{trip_id}`: the GTFS service date, the GTFS time of
the trip's first departure (past 24:00 after midnight) and the dataset's own trip ID prefixed
with the tag the instance gave the dataset. A stop is `{tag}_{stop_id}`. Tags differ per instance
(Transitous `cz-CZPTT`, motis.obehy.cz `czptt`) and trip IDs contain `_`, so the tags are
configured (`data/api/motis-datasets-v1.toml`) and matched as prefixes, never split on `_`.
"""

from __future__ import annotations

import re
import tomllib
from dataclasses import dataclass
from datetime import date
from functools import cache
from pathlib import Path
from typing import Any, Literal, cast

DATASETS = Path(__file__).resolve().parents[1] / "data" / "api" / "motis-datasets-v1.toml"

Resolve = Literal["trip_id", "source_key"]
_TRIP = re.compile(r"(?P<date>\d{8})_(?P<start>\d{1,3}:\d{2})_(?P<rest>.+)")


@dataclass(frozen=True, slots=True)
class Dataset:
    tag: str
    resolve: Resolve
    trip_namespace: str | None = None
    stop_namespace: str | None = None


@dataclass(frozen=True, slots=True)
class MotisTrip:
    """A MOTIS trip ID taken apart. `dataset` is None for a tag that is not configured; `trip_id`
    is then everything after the start time."""

    service_date: date
    start: str
    dataset: Dataset | None
    trip_id: str


@dataclass(frozen=True, slots=True)
class MotisStop:
    dataset: Dataset
    stop_id: str


def load_datasets(path: Path = DATASETS) -> tuple[Dataset, ...]:
    """The configured datasets, longest tag first, so no tag is shadowed by one it starts with."""

    with path.open("rb") as stream:
        document = tomllib.load(stream)
    datasets: list[Dataset] = []
    for raw in cast(list[dict[str, Any]], document["dataset"]):
        resolve = str(raw["resolve"])
        if resolve not in ("trip_id", "source_key"):
            raise ValueError(f"{path}: dataset {raw['tag']!r} has resolve {resolve!r}")
        dataset = Dataset(
            tag=str(raw["tag"]),
            resolve=resolve,
            trip_namespace=raw.get("trip_namespace"),
            stop_namespace=raw.get("stop_namespace"),
        )
        if dataset.resolve == "source_key" and dataset.trip_namespace is None:
            raise ValueError(f"{path}: dataset {dataset.tag!r} resolves by key without namespace")
        datasets.append(dataset)
    tags = [d.tag for d in datasets]
    if len(set(tags)) != len(tags):
        raise ValueError(f"{path}: a dataset tag is listed twice")
    return tuple(sorted(datasets, key=lambda d: (-len(d.tag), d.tag)))


@cache
def datasets() -> tuple[Dataset, ...]:
    return load_datasets()


def _dataset_of(value: str, known: tuple[Dataset, ...]) -> tuple[Dataset, str] | None:
    for dataset in known:
        prefix = f"{dataset.tag}_"
        if value.startswith(prefix) and len(value) > len(prefix):
            return dataset, value[len(prefix) :]
    return None


def parse_trip_id(value: str, known: tuple[Dataset, ...]) -> MotisTrip | None:
    """The parts of a MOTIS trip ID, or None when it is not one."""

    found = _TRIP.fullmatch(value)
    if found is None:
        return None
    raw = found["date"]
    try:
        service_date = date(int(raw[:4]), int(raw[4:6]), int(raw[6:]))
    except ValueError:
        return None
    rest = found["rest"]
    tagged = _dataset_of(rest, known)
    if tagged is None:
        return MotisTrip(service_date, found["start"], None, rest)
    return MotisTrip(service_date, found["start"], tagged[0], tagged[1])


def parse_stop_id(value: str, known: tuple[Dataset, ...]) -> MotisStop | None:
    """A MOTIS stop ID of a configured dataset, or None."""

    tagged = _dataset_of(value, known)
    return None if tagged is None else MotisStop(*tagged)
