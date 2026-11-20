"""How a train's composition is shown (`compositions.py`): decided here once, so the web and the
app draw the same train.

- A segment's vehicles in train order, each with its cab ends (which ends are drawn as a cab)
  and whether it runs only part of the segment (it joins or leaves within it).
- Stretches: neighbouring segments running the same train (the same vehicles, in either order:
  ČD answers from some stations back to front, CD-Q8) shown as one.
- Where the rider is: the segment holding a call (a call ending one segment and starting the
  next is in the next: one boards there), and the stretch holding that segment.
- A span's end named as a rider knows it: past the trip's last public call (a depot after the
  terminus) it reads as that call, before the first as the first.
"""

from __future__ import annotations

from collections.abc import Sequence

from obehy.api import schema


def cab_ends(units: Sequence[schema.CompositionUnit]) -> list[schema.CabEnds]:
    """The cab ends of a train's vehicles in order. Where the source names a car's cab side
    (`details.cab`: front, rear, both), that is it. A locomotive's cab is at its end of the
    train (both when it runs alone). A cab without a side (`true`) is placed: on a multiple
    unit, the first such car of a unit faces forward and the next closes it; a control car
    shows it at the end of the train it is at, inside the train away from its set's
    locomotive."""

    last = len(units) - 1
    locomotives = [n for n, u in enumerate(units) if u.kind == "locomotive"]
    opened = False  # inside a multiple unit whose front cab has been drawn
    ends: list[schema.CabEnds] = []
    for n, unit in enumerate(units):
        cab = unit.details.get("cab")
        if unit.kind != "unit":
            opened = False
        if cab in ("front", "rear", "both"):
            if unit.kind == "unit":
                opened = cab == "front"
            ends.append(schema.CabEnds(front=cab != "rear", rear=cab != "front"))
        elif unit.kind == "locomotive":
            ends.append(schema.CabEnds(front=n == 0, rear=n == last))
        elif cab != "true":
            ends.append(schema.CabEnds(front=False, rear=False))
        elif unit.kind == "unit":
            opened = not opened
            ends.append(schema.CabEnds(front=opened, rear=not opened))
        else:
            nearest = min(locomotives, key=lambda i: abs(i - n), default=None)
            front = n == 0 or (n != last and (n <= last / 2 if nearest is None else nearest > n))
            ends.append(schema.CabEnds(front=front, rear=not front))
    return ends


def vehicles(
    units: Sequence[schema.CompositionUnit], positions: Sequence[int], start: int, end: int
) -> list[schema.SegmentVehicle]:
    """A segment's vehicles (`positions` in `units`, train order) between the calls `start` and
    `end` (sequences): each with its cab ends and whether it runs only part of the segment."""

    own = [units[p] for p in positions]
    return [
        schema.SegmentVehicle(
            unit=p,
            cab=cab,
            partial=unit.from_call.sequence > start or unit.to_call.sequence < end,
        )
        for p, unit, cab in zip(positions, own, cab_ends(own), strict=True)
    ]


def _signature(unit: schema.CompositionUnit) -> tuple[object, ...]:
    """What makes two vehicles the same to a rider (not when or from where they were stated)."""

    return (
        unit.kind,
        unit.vehicle_type,
        unit.coach_number,
        tuple(unit.classes),
        tuple(sorted(f.feature for f in unit.features)),
        unit.destination,
    )


def _same_train(a: Sequence[schema.CompositionUnit], b: Sequence[schema.CompositionUnit]) -> bool:
    x, y = [_signature(u) for u in a], [_signature(u) for u in b]
    return x == y or x == y[::-1]


def stretches(
    units: Sequence[schema.CompositionUnit], segments: Sequence[schema.CompositionSegment]
) -> list[schema.CompositionStretch]:
    """The stretches to show: neighbouring segments running the same train are one."""

    def train(segment: schema.CompositionSegment) -> list[schema.CompositionUnit]:
        return [units[v.unit] for v in segment.vehicles]

    shown: list[schema.CompositionStretch] = []
    for n, segment in enumerate(segments):
        previous = shown[-1] if shown else None
        if previous is not None and _same_train(
            train(segments[previous.segments[-1]]), train(segment)
        ):
            shown[-1] = previous.model_copy(
                update={"to_call": segment.to_call, "segments": [*previous.segments, n]}
            )
        else:
            shown.append(
                schema.CompositionStretch(
                    from_call=segment.from_call, to_call=segment.to_call, segments=[n]
                )
            )
    return shown


def segment_at(segments: Sequence[schema.CompositionSegment], sequence: int | None) -> int | None:
    """The segment holding the call `sequence`, else the first; the last for the terminus.
    A call ending one segment and starting the next is in the next."""

    if not segments:
        return None
    if sequence is None:
        return 0
    for n, s in enumerate(segments):
        if s.from_call.sequence <= sequence < s.to_call.sequence:
            return n
    return 0 if sequence < segments[0].from_call.sequence else len(segments) - 1


def stretch_of(stretches_: Sequence[schema.CompositionStretch], segment: int | None) -> int | None:
    """The stretch holding a segment."""

    return next((n for n, s in enumerate(stretches_) if segment in s.segments), None)


def public_name(
    call: schema.CompositionCall, first: schema.CompositionCall, last: schema.CompositionCall
) -> schema.CompositionCall:
    """A span end as a rider knows it: past the last public call that call's name, before the
    first the first's."""

    if call.sequence > last.sequence:
        return call.model_copy(update={"name": last.name})
    if call.sequence < first.sequence:
        return call.model_copy(update={"name": first.name})
    return call
