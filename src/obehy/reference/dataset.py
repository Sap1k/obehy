"""A curated vehicle dataset as importers produce it and `load` mirrors it: models, vehicles,
the keys they are recognised by (a source's vehicle ID or a licence plate) and attributes.
Pure."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from obehy.reference.vocabulary import Vocabulary


@dataclass(frozen=True, slots=True)
class Model:
    vehicle_model_id: str
    model: str
    manufacturer: str | None = None
    mode: str | None = None


@dataclass(frozen=True, slots=True)
class Vehicle:
    vehicle_id: str
    vehicle_model_id: str | None
    public_label: str | None = None


@dataclass(frozen=True, slots=True)
class Key:
    """How a vehicle is recognised from `valid_from` (to `valid_to`): a source's vehicle ID,
    or an identifier (a licence plate, an EVN, a series number) whichever source states it."""

    vehicle_id: str
    valid_from: date
    valid_to: date | None = None
    source: str | None = None
    source_vehicle_id: str | None = None
    scheme: str | None = None
    identifier: str | None = None


@dataclass(frozen=True, slots=True)
class Dataset:
    name: str
    version: str
    title: str
    private: bool
    # Where datasets disagree the lower wins: our own list (0) over seeds (10).
    priority: int
    models: list[Model] = field(default_factory=list[Model])
    vehicles: list[Vehicle] = field(default_factory=list[Vehicle])
    keys: list[Key] = field(default_factory=list[Key])
    # (vehicle_id, attribute, value) and (vehicle_model_id, attribute, value)
    attributes: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])
    model_attributes: list[tuple[str, str, str]] = field(default_factory=list[tuple[str, str, str]])

    def check(self, vocabulary: Vocabulary) -> None:
        """Raise on an attribute outside the vocabulary, a dangling reference, a malformed
        key or a duplicate."""

        models = {m.vehicle_model_id for m in self.models}
        vehicles = {v.vehicle_id for v in self.vehicles}
        if len(models) != len(self.models) or len(vehicles) != len(self.vehicles):
            raise ValueError(f"{self.name}: duplicate model or vehicle IDs")
        for v in self.vehicles:
            if v.vehicle_model_id is not None and v.vehicle_model_id not in models:
                raise ValueError(f"{self.name}: {v.vehicle_id} names an unknown model")
        seen: set[tuple[str | None, str | None, str | None, str | None, date]] = set()
        for k in self.keys:
            if k.vehicle_id not in vehicles:
                raise ValueError(f"{self.name}: a key of unknown vehicle {k.vehicle_id}")
            by_source = k.source is not None and k.source_vehicle_id is not None
            if by_source == (k.scheme is not None and k.identifier is not None):
                raise ValueError(
                    f"{self.name}: {k.vehicle_id}: a key is a source ID or an identifier"
                )
            if (
                k.scheme is not None
                and k.identifier is not None
                and k.identifier != vocabulary.normalise(k.scheme, k.identifier)
            ):
                raise ValueError(f"{self.name}: {k.scheme} {k.identifier!r} is not normalised")
            identity = (k.source, k.source_vehicle_id, k.scheme, k.identifier, k.valid_from)
            if identity in seen:
                raise ValueError(f"{self.name}: key {identity} given twice")
            seen.add(identity)
        for vehicle_id, attribute, value in self.attributes:
            if vehicle_id not in vehicles:
                raise ValueError(f"{self.name}: attribute of unknown vehicle {vehicle_id}")
            vocabulary.check(attribute, value)
        for model_id, attribute, value in self.model_attributes:
            if model_id not in models:
                raise ValueError(f"{self.name}: attribute of unknown model {model_id}")
            vocabulary.check(attribute, value)
        if len({(a[0], a[1]) for a in self.attributes}) != len(self.attributes):
            raise ValueError(f"{self.name}: an attribute is given twice for one vehicle")

    @property
    def rows(self) -> int:
        return (
            len(self.models)
            + len(self.vehicles)
            + len(self.keys)
            + len(self.attributes)
            + len(self.model_attributes)
        )
