"""The vehicle registry in memory (BASE_PLAN.md section 27): which vehicle a source's report is,
and what the curated datasets say about it.

A report is a registered vehicle when a key valid on the day binds it: its source's vehicle ID
first, else an identifier it states (licence plate, EVN, series number: any source), in the
vocabulary's order. Where two datasets' keys claim it, the better dataset (lower priority)
wins. An unbound report is its own vehicle, `<source>:<source_vehicle_id>`. The registry binds
only by these curated keys (co-located reports are paired elsewhere, emit/pairing.py).
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import Literal

import psycopg

from obehy.reference.vocabulary import Vocabulary, load_vocabulary

BoundBy = Literal["source", "identifier", "co_located", "train"]
# `obehy ref import` notifies the realtime worker here after loading a dataset.
REGISTRY_CHANNEL = "obehy_registry"


@dataclass(frozen=True, slots=True)
class RegistryKey:
    vehicle_id: str
    priority: int
    valid_from: date
    valid_to: date | None

    def valid(self, day: date) -> bool:
        return self.valid_from <= day and (self.valid_to is None or day <= self.valid_to)


@dataclass(frozen=True, slots=True)
class CuratedValue:
    attribute: str
    value: str
    dataset: str
    priority: int
    origin: Literal["curated", "model"]


@dataclass(frozen=True, slots=True)
class Entry:
    """A registered vehicle: its best label and model, and the datasets describing it."""

    vehicle_id: str
    public_label: str | None
    manufacturer: str | None
    model: str | None
    datasets: tuple[str, ...]


@dataclass(slots=True)
class Registry:
    by_source: dict[tuple[str, str], list[RegistryKey]] = field(
        default_factory=dict[tuple[str, str], list[RegistryKey]]
    )
    # (scheme, normalised identifier) -> keys
    by_identifier: dict[tuple[str, str], list[RegistryKey]] = field(
        default_factory=dict[tuple[str, str], list[RegistryKey]]
    )
    entries: dict[str, Entry] = field(default_factory=dict[str, Entry])
    values: dict[str, list[CuratedValue]] = field(default_factory=dict[str, list[CuratedValue]])
    vocabulary: Vocabulary = field(default_factory=load_vocabulary)

    def bound(
        self, source: str, source_vehicle_id: str, stated: Mapping[str, str], day: date
    ) -> tuple[str, BoundBy] | None:
        """The registered vehicle a report is on `day`, by its source key or by an identifier
        it states (`stated`: scheme -> value as stated), and how; None when no key binds it."""

        found = _best(self.by_source.get((source, source_vehicle_id), ()), day)
        if found is not None:
            return found, "source"
        for scheme in self.vocabulary.identifiers:
            value = stated.get(scheme)
            if value is None:
                continue
            key = (scheme, self.vocabulary.normalise(scheme, value))
            found = _best(self.by_identifier.get(key, ()), day)
            if found is not None:
                return found, "identifier"
        return None


def _best(keys: list[RegistryKey] | tuple[RegistryKey, ...], day: date) -> str | None:
    valid = [k for k in keys if k.valid(day)]
    return min(valid, key=lambda k: (k.priority, k.vehicle_id)).vehicle_id if valid else None


def load_registry(connection: psycopg.Connection) -> Registry:
    """The registry as `ref.*` holds it now."""

    registry = Registry()
    by_source: defaultdict[tuple[str, str], list[RegistryKey]] = defaultdict(list)
    by_identifier: defaultdict[tuple[str, str], list[RegistryKey]] = defaultdict(list)
    for (
        vehicle_id,
        source,
        source_vehicle_id,
        scheme,
        identifier,
        since,
        until,
        priority,
    ) in connection.execute(
        "SELECT k.vehicle_id, k.source, k.source_vehicle_id, k.scheme, k.identifier,"
        " k.valid_from, k.valid_to, d.priority"
        " FROM ref.vehicle_key k JOIN ref.dataset d USING (dataset) ORDER BY 1, 2, 3, 4, 5, 6"
    ).fetchall():
        key = RegistryKey(vehicle_id, priority, since, until)
        if source is not None:
            by_source[(source, source_vehicle_id)].append(key)
        else:
            by_identifier[(scheme, identifier)].append(key)
    registry.by_source, registry.by_identifier = dict(by_source), dict(by_identifier)
    for vehicle_id, label, manufacturer, model, datasets in connection.execute(
        "SELECT x.vehicle_id,"
        " (array_agg(x.public_label ORDER BY d.priority)"
        "  FILTER (WHERE x.public_label IS NOT NULL))[1],"
        " (array_agg(m.manufacturer ORDER BY d.priority)"
        "  FILTER (WHERE m.manufacturer IS NOT NULL))[1],"
        " (array_agg(m.model ORDER BY d.priority) FILTER (WHERE m.model IS NOT NULL))[1],"
        " array_agg(x.dataset ORDER BY d.priority, x.dataset)"
        " FROM ref.vehicle x JOIN ref.dataset d USING (dataset)"
        " LEFT JOIN ref.vehicle_model m"
        "  ON m.vehicle_model_id = x.vehicle_model_id AND m.dataset = x.dataset"
        " GROUP BY x.vehicle_id ORDER BY x.vehicle_id"
    ).fetchall():
        registry.entries[vehicle_id] = Entry(
            vehicle_id, label, manufacturer, model, tuple(datasets)
        )
    values: defaultdict[str, list[CuratedValue]] = defaultdict(list)
    for vehicle_id, attribute, value, dataset, priority, origin in connection.execute(
        "SELECT a.vehicle_id, a.attribute, a.value, a.dataset, d.priority, 'curated'"
        " FROM ref.vehicle_attribute a JOIN ref.dataset d USING (dataset)"
        " UNION ALL"
        " SELECT x.vehicle_id, m.attribute, m.value, m.dataset, d.priority, 'model'"
        " FROM ref.vehicle x JOIN ref.model_attribute m"
        "  ON m.vehicle_model_id = x.vehicle_model_id AND m.dataset = x.dataset"
        " JOIN ref.dataset d ON d.dataset = m.dataset"
        " UNION ALL"
        " SELECT x.vehicle_id, v.attribute, v.value, mo.dataset, d.priority, 'model'"
        " FROM ref.vehicle x JOIN ref.vehicle_model mo"
        "  ON mo.vehicle_model_id = x.vehicle_model_id AND mo.dataset = x.dataset"
        " JOIN ref.dataset d ON d.dataset = mo.dataset"
        " CROSS JOIN LATERAL (VALUES ('manufacturer', mo.manufacturer), ('model', mo.model))"
        "  AS v (attribute, value) WHERE v.value IS NOT NULL"
        " ORDER BY 1, 2, 5, 4"
    ).fetchall():
        values[vehicle_id].append(CuratedValue(attribute, value, dataset, priority, origin))
    registry.values = dict(values)
    return registry
