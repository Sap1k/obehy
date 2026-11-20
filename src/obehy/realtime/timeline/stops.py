"""Where a road vehicle is seen at a stop: the fixes there, whatever the reading makes of them.

Map matching places a fix along the path; stop events come from where the reading crosses the
trigger points (`commit`). Fixes at a stop do not always reach the reading: a vehicle coming
to a stop from the far side (a detour) is first placed past it and cannot step back, a stop
bay off the shape is held off the path, and a vehicle standing in GPS noise creeps forward
over the departure trigger. So the fixes within the departure margin of a call's point
(`StopSeen`) bound its events directly:

- the vehicle arrived by the first fix there;
- it left after the last fix there and before the first fix away (`away`): no departure is
  committed while the newest fix is still at the stop, and progress stays short of the
  departure trigger meanwhile;
- the departure lies in that gap: a typical dwell (the mode's `dwell_credit_s`) after the last
  fix there at most, never past the middle of the gap, and early enough to have driven to the
  first fix away at the timetable's pace (`departure`). The estimate shows that time once the
  vehicle is seen away, so the shown departure is the one committed.

Calls in a row at one point (a layover split into an arrival and a departure) are one dwell at
the last of them.

Terminals: bus stations and stands often lie hundreds of metres from the coordinates the
timetable gives the first or last stop, off the path. There a fix within `terminal_radius_m`
counts while the vehicle stands (it moved less than `standing_m` since its previous fix): the
first stop's departure waits while it stands on the stand and is timed by the last standing fix
and the first away; at the last stop, the start of the final standstill is its arrival when the
journey ends without one (`core._arrived_unseen`).
"""

from __future__ import annotations

from datetime import timedelta

from obehy.realtime.geo import distance_m
from obehy.realtime.model import StopSeen
from obehy.realtime.timeline.path import point_at
from obehy.realtime.timeline.plan import Plan
from obehy.realtime.times import Instant

AHEAD = 3  # calls looked at past the first one not yet left


def _pending(plan: Plan, committed_m: float) -> int | None:
    """The first call whose departure trigger is not committed yet; past the last one, the
    terminus while its arrival is not."""

    for call, zone in enumerate(plan.zones):
        if zone.departure_m is not None and zone.departure_m > committed_m:
            return call
    last = len(plan.zones) - 1
    arrival = plan.zones[last].arrival_m if plan.zones else None
    return last if arrival is not None and arrival > committed_m else None


def done_at(plan: Plan, call: int, committed_m: float) -> bool:
    """Whether the call's last event (its departure; the terminus's arrival) is committed."""

    zone = plan.zones[call]
    trigger = zone.departure_m if zone.departure_m is not None else zone.arrival_m
    return trigger is None or trigger <= committed_m


def _last_at_point(plan: Plan, call: int) -> int:
    distances = plan.path.call_distances_m
    while call + 1 < len(distances) - 1 and distances[call + 1] == distances[call]:
        call += 1
    return call


def at_call(
    plan: Plan,
    committed_m: float,
    here: tuple[float, float],
    radius_m: float,
    terminal_m: float = 0.0,
) -> int | None:
    """The call whose point the fix is within `radius_m` of, among the first calls not yet
    left (the last one at its point); at the first and last call within `terminal_m` (a
    vehicle standing on a terminal's stand, given only while it stands). None elsewhere."""

    first = _pending(plan, committed_m)
    if first is None:
        return None
    distances = plan.path.call_distances_m
    last = len(distances) - 1
    for call in range(first, min(first + AHEAD, last + 1)):
        reach = max(radius_m, terminal_m) if call in (0, last) else radius_m
        if distance_m(here, point_at(plan.path, distances[call])) <= reach:
            return _last_at_point(plan, call) if call < last else call
    return None


def seen(
    plan: Plan,
    previous: StopSeen | None,
    committed_m: float,
    here: tuple[float, float],
    at: Instant,
    radius_m: float,
    terminal_m: float = 0.0,
    still_since: Instant | None = None,
) -> StopSeen | None:
    """The stop presence after a fix at `here`: kept, extended, ended (seen away) or begun at
    another call. A presence whose call is done is dropped. `terminal_m` is the reach at the
    first and last call while the vehicle stands (0: it moves, or the source cannot tell);
    standing began at `still_since`, the previous fix, which a new presence starts from."""

    if previous is not None and done_at(plan, previous.call, committed_m):
        previous = None
    call = at_call(plan, committed_m, here, radius_m, terminal_m)
    if call is None:
        if previous is not None and previous.away is None:
            return StopSeen(previous.call, previous.first, previous.last, at)
        return previous
    if previous is not None and previous.call == call:
        if previous.away is not None and call == len(plan.zones) - 1:
            return StopSeen(call, still_since or at, at)  # standing again short of it
        return StopSeen(call, previous.first, at)
    if previous is not None and call < previous.call:
        return previous  # back at a call already left: noise
    return StopSeen(call, still_since or at, at)


def holding(stop: StopSeen | None) -> int | None:
    """The call whose departure waits: the vehicle's newest fix is still there."""

    return stop.call if stop is not None and stop.away is None else None


def departure(plan: Plan, stop: StopSeen, dwell_s: float) -> Instant:
    """When the vehicle left a stop it was seen at until `last` and away from at `away`."""

    assert stop.away is not None
    gap = stop.away - stop.last
    drive = timedelta(0)
    leaves = plan.scheduled_time(stop.call, "departure")
    if stop.away_m is not None and leaves is not None:
        reached = plan.timetable.planned(stop.away, stop.away_m)
        if reached is not None and reached > leaves:
            drive = reached - leaves
    credit = min(timedelta(seconds=dwell_s), gap / 2, gap - drive)
    return Instant(stop.last + max(credit, timedelta(0)))


def capped(plan: Plan, stop: StopSeen | None, along_m: float) -> float:
    """Progress short of the departure trigger of the call the vehicle is still at."""

    call = holding(stop)
    if call is None:
        return along_m
    trigger = plan.zones[call].departure_m
    return along_m if trigger is None or along_m < trigger else trigger - 1.0
