import type { Schemas } from "../../api/client";

// A train's composition (the API's `…/composition`): units with their spans over the trip's
// calls, segments with their vehicles (cab ends, partial), stretches and where the rider is,
// all decided by the API (`api/formations.py`). Here only how a unit is worded.

export type Composition = Schemas["Composition"];
export type CompositionUnit = Schemas["CompositionUnit"];
export type CabEnds = Schemas["CabEnds"];

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
