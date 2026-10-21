"""Two sources reporting one vehicle with nothing to bind them: the same journey, side by side
(BASE_PLAN.md section 27).

A DÚK bus the registry knows by its DÚK ID and the MPVnet report of its plate are one vehicle,
but until a key says so nothing binds them. When two reports from different sources run the
same journey with fixes close in time and place (`[pairing]` in the vehicle vocabulary), the
evidence builds up; once confirmed they are one vehicle (`bound_by = co_located`) while it
holds. They part when they are apart on that journey, when each runs another journey, or when
no close fix has come for `hold_s`. Every pairing is also kept for review as a registry key
candidate (`rt.vehicle_pairing`). Pure; the writer stores it.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field
from itertools import combinations

from obehy.realtime.geo import distance_m
from obehy.realtime.model import JourneyKey
from obehy.realtime.times import Instant
from obehy.reference.vocabulary import Pairing as Settings

Report = tuple[str, str]  # (source, source vehicle ID)
Pair = tuple[Report, Report]  # ordered: the lesser report first


@dataclass(frozen=True, slots=True)
class Fix:
    report: Report
    journey: JourneyKey
    lon: float
    lat: float
    at: Instant


@dataclass(slots=True)
class Evidence:
    first_seen: Instant
    last_seen: Instant
    fixes: int
    journey: JourneyKey
    confirmed: bool = False


@dataclass(slots=True)
class Pairings:
    settings: Settings
    pairs: dict[Pair, Evidence] = field(default_factory=dict[Pair, Evidence])
    # Pairs whose evidence changed since `changes()` was last asked (None: dropped).
    changed: dict[Pair, Evidence | None] = field(default_factory=dict[Pair, Evidence | None])

    def observe(self, fixes: list[Fix], now: Instant) -> set[Report]:
        """Take this tick's fixes; return the reports whose pairing was confirmed or dropped."""

        s = self.settings
        moved: set[Report] = set()
        journeys = {f.report: f.journey for f in fixes}
        by_journey: defaultdict[JourneyKey, list[Fix]] = defaultdict(list)
        for f in fixes:
            by_journey[f.journey].append(f)
        for journey, group in sorted(by_journey.items(), key=lambda kv: repr(kv[0])):
            for a, b in combinations(sorted(group, key=lambda f: f.report), 2):
                if (
                    a.report[0] == b.report[0]
                    or abs((a.at - b.at).total_seconds()) > s.max_fix_gap_s
                ):
                    continue
                pair = (a.report, b.report)
                gap = distance_m((a.lon, a.lat), (b.lon, b.lat))
                if gap > s.apart_m:
                    moved |= self._drop(pair)
                elif gap <= s.distance_m:
                    moved |= self._close(pair, journey, max(a.at, b.at))
        for pair, evidence in sorted(self.pairs.items()):
            ja, jb = journeys.get(pair[0]), journeys.get(pair[1])
            on_other_journeys = ja is not None and jb is not None and ja != jb
            if on_other_journeys or (now - evidence.last_seen).total_seconds() > s.hold_s:
                moved |= self._drop(pair)
        return moved

    def _close(self, pair: Pair, journey: JourneyKey, at: Instant) -> set[Report]:
        evidence = self.pairs.get(pair)
        if evidence is None:
            self.pairs[pair] = self.changed[pair] = Evidence(at, at, 1, journey)
            return set()
        if at <= evidence.last_seen:
            return set()  # the same fixes again
        evidence.last_seen, evidence.fixes, evidence.journey = at, evidence.fixes + 1, journey
        self.changed[pair] = evidence
        s = self.settings
        sustained = (evidence.last_seen - evidence.first_seen).total_seconds() >= s.confirm_s
        if not evidence.confirmed and evidence.fixes >= s.min_fixes and sustained:
            evidence.confirmed = True
            return set(pair)
        return set()

    def _drop(self, pair: Pair) -> set[Report]:
        evidence = self.pairs.pop(pair, None)
        if evidence is None:
            return set()
        self.changed[pair] = None
        return set(pair) if evidence.confirmed else set()

    def partners(self, report: Report) -> list[Report]:
        """The reports confirmed to be the same vehicle as `report`, sorted."""

        return sorted(
            (b if a == report else a)
            for (a, b), evidence in self.pairs.items()
            if evidence.confirmed and report in (a, b)
        )

    def changes(self) -> dict[Pair, Evidence | None]:
        changed, self.changed = self.changed, {}
        return changed
