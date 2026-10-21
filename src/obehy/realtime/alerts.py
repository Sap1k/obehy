"""Alerts from any source, in one shape (BASE_PLAN.md section 25).

A source's alerts are decoded into `Alert`s: cause and effect in GTFS-RT terms, the periods it
is active, texts per language, and the entities it informs as the source names them (a source
key namespace and identifier). Each poll states a channel's whole current set; the writer
(`emit/alerts.py`) keeps `rt.alert`, and SQL resolves the entities against the active release
per feed (`rt.alert_target`), so a new release re-resolves them. Alerts never pass through the
core and never cancel trips: an alert informs a scope, it does not state a trip's fate. Pure.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

from obehy.realtime.times import Instant

AlertEntityKind = Literal["route", "stop", "trip"]
# (language, text), sorted by language
Texts = tuple[tuple[str, str], ...]


@dataclass(frozen=True, slots=True)
class AlertEntity:
    """What an alert informs, as the source names it: an identifier in a source key namespace
    (`pid:gtfs_route_id`), resolved through `source_key`; a trip with its service date."""

    kind: AlertEntityKind
    namespace: str
    identifier: str
    service_date: date | None = None


@dataclass(frozen=True, slots=True)
class Period:
    start: Instant | None
    end: Instant | None  # None: until further notice


@dataclass(frozen=True, slots=True)
class Alert:
    source: str
    channel: str
    alert_id: str  # the source's, unique within the source
    cause: str  # a GTFS-RT `Alert.Cause` name
    effect: str  # a GTFS-RT `Alert.Effect` name
    severity: str  # a GTFS-RT `Alert.SeverityLevel` name
    periods: tuple[Period, ...]
    header: Texts
    description: Texts = ()
    url: Texts = ()
    cause_detail: Texts = ()
    effect_detail: Texts = ()
    entities: tuple[AlertEntity, ...] = ()
    # Source-specific, kept for the API (PID infotexts: how a board shows it).
    details: tuple[tuple[str, str], ...] = ()


def texts(**by_language: str | None) -> Texts:
    """The non-empty texts, sorted by language."""

    return tuple(sorted((lang, text) for lang, text in by_language.items() if text))
