from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True, order=True)
class PublicId:
    """An opaque serving identifier owned by the active static build.

    Oběhy deliberately does not interpret prefixes or impose a length limit. During the
    provisional phase JrUtil emits ``v0:...`` values; the later registry may emit compact
    surface IDs or country-scoped railway IDs without a database migration.
    """

    value: str

    def __post_init__(self) -> None:
        if not self.value or "\x00" in self.value:
            raise ValueError("Public IDs must be non-empty text without NUL characters")

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
