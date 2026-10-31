import type { Schemas, Trip } from "../../api/client";

// A train's composition (the API's `…/composition`): units with their spans over the trip's
// calls, and segments, the trip split wherever a unit joins or leaves. The strip shows one
// segment: the one a rider boards in, else the one the train is in now.

export type Composition = Schemas["Composition"];
export type CompositionUnit = Schemas["CompositionUnit"];
export type CompositionSegment = Schemas["CompositionSegment"];

/** The segment holding the call at `locationId` (a call of this trip), else the first. A call
 * that ends one segment and starts the next is in the next: one boards there. */
export function segmentAt(
  composition: Composition,
  trip: Pick<Trip, "calls">,
  locationId: string | undefined,
): number {
  const { segments } = composition;
  if (segments.length === 0) return -1;
  const sequence = trip.calls.find((c) => c.location_id === locationId)?.sequence;
  if (sequence === undefined) return 0;
  const at = segments.findIndex(
    (s) => s.from_call.sequence <= sequence && sequence < s.to_call.sequence,
  );
  if (at >= 0) return at;
  return sequence < segments[0]!.from_call.sequence ? 0 : segments.length - 1;
}

/** A segment's units in train order. */
export function unitsOf(composition: Composition, segment: CompositionSegment) {
  return segment.units.flatMap((n) => composition.units[n] ?? []);
}

/** Whether a unit runs only part of the segment shown (it joins or leaves within it). */
export function partOfSegment(unit: CompositionUnit, segment: CompositionSegment): boolean {
  return (
    unit.from_call.sequence > segment.from_call.sequence ||
    unit.to_call.sequence < segment.to_call.sequence
  );
}

/** A unit's class as one label: "1", "2", "1/2", or null (a locomotive, a coach without
 * seats). */
export function classLabel(unit: CompositionUnit): string | null {
  return unit.classes.length ? [...unit.classes].sort().join("/") : null;
}

/** A unit's name for screen readers and the detail: "Vůz 367, 2. třída". */
export function unitName(unit: CompositionUnit): string {
  if (unit.kind === "locomotive") return "Lokomotiva";
  const head = unit.coach_number ? `Vůz ${unit.coach_number}` : "Vůz";
  const cls = classLabel(unit);
  return cls ? `${head}, ${cls}. třída` : head;
}

/** A feature's text: our label, else the source's own wording, else its code. */
export function featureText(feature: CompositionUnit["features"][number]): string {
  return feature.label?.cs ?? feature.text ?? feature.feature.replace(/^raw:[^:]+:/, "");
}

/** Which ends of a vehicle are drawn as a cab (slanted): `front` the left, toward the head of
 * the train, `rear` the right. */
export type Ends = { front: boolean; rear: boolean };

/** The cab ends of a train's vehicles in order. Where the source names a car's cab side
 * (`details.cab`: `front`, `rear`, `both`), that is it. A locomotive's cab is at its end of
 * the train (both when it runs alone). A cab without a side (`true`) is placed: on a multiple
 * unit, the first such car of a unit faces forward and the next closes it; a control car shows
 * it at the end of the train it is at, inside the train away from its set's locomotive. */
export function cabEnds(units: CompositionUnit[]): Ends[] {
  const last = units.length - 1;
  const locos = units.flatMap((u, i) => (u.kind === "locomotive" ? [i] : []));
  let open = false; // inside a multiple unit whose front cab has been drawn
  return units.map((unit, n) => {
    const cab = unit.details.cab;
    if (unit.kind !== "unit") open = false;
    if (cab === "front" || cab === "rear" || cab === "both") {
      if (unit.kind === "unit") open = cab === "front";
      return { front: cab !== "rear", rear: cab !== "front" };
    }
    if (unit.kind === "locomotive") return { front: n === 0, rear: n === last };
    if (cab !== "true") return { front: false, rear: false };
    if (unit.kind === "unit") {
      open = !open;
      return { front: open, rear: !open };
    }
    const nearest = [...locos].sort((a, b) => Math.abs(a - n) - Math.abs(b - n))[0];
    const front = n === 0 || (n !== last && (nearest === undefined ? n <= last / 2 : nearest > n));
    return { front, rear: !front };
  });
}

/** A stretch of the trip as shown: adjacent segments whose vehicles are the same merged into
 * one (`members`: the segments' positions in `composition.segments`). */
export type Shown = {
  from_call: CompositionSegment["from_call"];
  to_call: CompositionSegment["to_call"];
  members: number[];
};

/** What makes two vehicles the same to a rider (not when or from where they were stated). */
function signature(unit: CompositionUnit): string {
  const features = unit.features.map((f) => f.feature).sort();
  return JSON.stringify([
    unit.kind,
    unit.vehicle_type,
    unit.coach_number,
    unit.classes,
    features,
    unit.destination,
  ]);
}

function sameTrain(a: CompositionUnit[], b: CompositionUnit[]): boolean {
  const x = a.map(signature);
  const y = b.map(signature);
  if (x.length !== y.length) return false;
  const reversed = [...y].reverse();
  // The same vehicles in the same order, or reversed (ČD answers from some stations back to
  // front, CD-Q8).
  return x.every((s, i) => s === y[i]) || x.every((s, i) => s === reversed[i]);
}

/** The segments to show: neighbours running the same train are one. */
export function shownSegments(composition: Composition): Shown[] {
  const shown: Shown[] = [];
  composition.segments.forEach((segment, n) => {
    const previous = shown.at(-1);
    const before = previous && composition.segments[previous.members.at(-1)!]!;
    if (
      previous &&
      before &&
      sameTrain(unitsOf(composition, before), unitsOf(composition, segment))
    ) {
      previous.to_call = segment.to_call;
      previous.members.push(n);
    } else {
      shown.push({ from_call: segment.from_call, to_call: segment.to_call, members: [n] });
    }
  });
  return shown;
}

/** A segment end's name as a rider knows it: a span can end at a call past the trip's last
 * public one (a depot after the terminus), which reads as that terminus. */
export function callName(trip: Pick<Trip, "calls">, call: CompositionSegment["from_call"]): string {
  const first = trip.calls[0];
  const last = trip.calls.at(-1);
  if (last && call.sequence > last.sequence) return last.name;
  if (first && call.sequence < first.sequence) return first.name;
  return call.name;
}
