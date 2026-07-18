from __future__ import annotations

import re
from dataclasses import dataclass
from enum import StrEnum


class EntityKind(StrEnum):
    STOP_PLACE = "stop_place"
    BOARDING_POINT = "boarding_point"
    OPERATIONAL_POINT = "operational_point"
    ROUTE = "route"
    SCHEDULED_TRIP = "scheduled_trip"


PREFIX_BY_KIND: dict[EntityKind, str] = {
    EntityKind.STOP_PLACE: "S",
    EntityKind.BOARDING_POINT: "P",
    EntityKind.OPERATIONAL_POINT: "O",
    EntityKind.ROUTE: "R",
    EntityKind.SCHEDULED_TRIP: "T",
}
KIND_BY_PREFIX = {prefix: kind for kind, prefix in PREFIX_BY_KIND.items()}
CANONICAL_ID_PATTERN = re.compile(r"^(?P<prefix>[SPORT])(?P<number>[0-9]{9})$")


@dataclass(frozen=True, slots=True, order=True)
class CanonicalId:
    value: str

    def __post_init__(self) -> None:
        match = CANONICAL_ID_PATTERN.fullmatch(self.value)
        if match is None or int(match.group("number")) == 0:
            raise ValueError(f"Invalid canonical ID: {self.value!r}")

    @property
    def kind(self) -> EntityKind:
        return KIND_BY_PREFIX[self.value[0]]

    @classmethod
    def from_number(cls, kind: EntityKind, number: int) -> CanonicalId:
        if not 1 <= number <= 999_999_999:
            raise ValueError("Canonical ID number must be between 1 and 999999999")
        return cls(f"{PREFIX_BY_KIND[kind]}{number:09d}")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class CisLineId:
    value: str

    def __post_init__(self) -> None:
        if re.fullmatch(r"[0-9]{6}", self.value) is None:
            raise ValueError("CISLineID must contain exactly six digits")

    def __str__(self) -> str:
        return self.value


@dataclass(frozen=True, slots=True, order=True)
class CisTripId:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or self.value < 0:
            raise ValueError("CISTripID must be a non-negative integer")


@dataclass(frozen=True, slots=True, order=True)
class TrainNumber:
    value: int

    def __post_init__(self) -> None:
        if isinstance(self.value, bool) or self.value <= 0:
            raise ValueError("Train number must be a positive integer")
