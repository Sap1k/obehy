import { describe, expect, it } from "vitest";
import {
  cabEnds,
  callName,
  classLabel,
  featureText,
  partOfSegment,
  segmentAt,
  shownSegments,
  unitName,
  unitsOf,
  type Composition,
  type CompositionUnit,
} from "./composition";

const call = (sequence: number, name: string) => ({
  sequence,
  location_id: `czptt:location:${name}`,
  name,
});
const CHEB = call(1, "Cheb");
const MOST = call(3, "Most");
const USTI = call(5, "Ústí");
const PRAHA = call(7, "Praha");

const unit = (sequence: number, over: Partial<CompositionUnit> = {}): CompositionUnit => ({
  source: "cd",
  label: { cs: "ČD", en: "ČD" },
  sequence,
  kind: "coach",
  role: "train",
  vehicle_id: null,
  vehicle_type: "Bmpz 895",
  coach_number: String(360 + sequence),
  classes: [2],
  features: [],
  destination: null,
  details: {},
  from_call: CHEB,
  to_call: PRAHA,
  open: true,
  observed_at: "2026-10-20T09:00:00+02:00",
  ...over,
});

// 650.002 the whole way; 650.003 attached between Most and Ústí.
const COMPOSITION = {
  units: [unit(1), unit(2, { from_call: MOST, to_call: USTI })],
  segments: [
    { from_call: CHEB, to_call: MOST, units: [0] },
    { from_call: MOST, to_call: USTI, units: [0, 1] },
    { from_call: USTI, to_call: PRAHA, units: [0] },
  ],
} as unknown as Composition;

const TRIP = { calls: [CHEB, call(2, "Sokolov"), MOST, call(4, "x"), USTI, call(6, "y"), PRAHA] };

describe("segmentAt", () => {
  it("finds the segment a call is in; a boundary call starts the next", () => {
    expect(segmentAt(COMPOSITION, 2)).toBe(0); // Sokolov
    expect(segmentAt(COMPOSITION, MOST.sequence)).toBe(1);
    expect(segmentAt(COMPOSITION, 6)).toBe(2);
  });

  it("falls back to the first segment, and the last for the terminus", () => {
    expect(segmentAt(COMPOSITION, undefined)).toBe(0);
    expect(segmentAt(COMPOSITION, PRAHA.sequence)).toBe(2);
    expect(segmentAt({ ...COMPOSITION, segments: [] }, undefined)).toBe(-1);
  });
});

describe("units", () => {
  it("lists a segment's units and tells those running only part of it", () => {
    const middle = COMPOSITION.segments[1];
    expect(unitsOf(COMPOSITION, middle!).map((u) => u.sequence)).toEqual([1, 2]);
    expect(partOfSegment(COMPOSITION.units[1]!, middle!)).toBe(false);
    expect(partOfSegment(unit(3, { to_call: MOST }), middle!)).toBe(true);
  });

  it("names units and their classes", () => {
    expect(classLabel(unit(1, { classes: [2, 1] }))).toBe("1/2");
    expect(unitName(unit(1, { classes: [1] }))).toBe("Vůz 361, 1. třída");
    expect(unitName(unit(1, { kind: "locomotive", classes: [], coach_number: null }))).toBe(
      "Lokomotiva",
    );
  });

  it("words a feature by our label, else the source's text", () => {
    const wifi = { feature: "wifi", label: { cs: "Wi-Fi", en: "Wi-Fi" }, icon: "wifi", text: null };
    const raw = { feature: "raw:cd:kolo", label: null, icon: null, text: "Kolo" };
    expect(featureText(wifi)).toBe("Wi-Fi");
    expect(featureText(raw)).toBe("Kolo");
    expect(featureText({ ...raw, text: null })).toBe("kolo");
  });
});

describe("cabEnds", () => {
  const loco = unit(0, { kind: "locomotive" });
  const coach = unit(1);
  const control = unit(2, { details: { cab: "true" } });
  const car = (cab: boolean) => unit(3, { kind: "unit", details: cab ? { cab: "true" } : {} });
  const shown = (units: CompositionUnit[]) =>
    cabEnds(units).map(({ front, rear }) => (front ? "<" : "") + (rear ? ">" : "") || "-");

  it("slants a locomotive at its end of the train, a control car always", () => {
    expect(shown([loco, coach, coach, control])).toEqual(["<", "-", "-", ">"]);
    expect(shown([control, coach, loco])).toEqual(["<", "-", ">"]);
    expect(shown([loco])).toEqual(["<>"]);
    // Two push-pull sets coupled cab to cab: each control car away from its locomotive.
    expect(shown([loco, coach, control, control, coach, loco])).toEqual([
      "<",
      "-",
      ">",
      "<",
      "-",
      ">",
    ]);
  });

  it("slants a multiple unit's cab where the source names its side", () => {
    // Regionova trio and pair (train 1584): 814 front, 014 none, 814 rear | 814 front, 914 rear.
    const side = (cab?: string) => unit(3, { kind: "unit", details: cab ? { cab } : {} });
    expect(shown([side("front"), side(), side("rear"), side("front"), side("rear")])).toEqual([
      "<",
      "-",
      ">",
      "<",
      ">",
    ]);
    expect(shown([coach, side("both")])).toEqual(["-", "<>"]);
  });

  it("places a unit's cabs without a side: the first opens, the next closes", () => {
    // Two InterPanters coupled (`661l` … `660L` twice).
    const two = [car(true), car(false), car(true), car(true), car(false), car(true)];
    expect(shown(two)).toEqual(["<", "-", ">", "<", "-", ">"]);
    expect(shown([car(false), car(false)])).toEqual(["-", "-"]);
  });
});

describe("shownSegments", () => {
  const a = unit(1);
  const b = unit(2, { classes: [1] });
  const seg = (from: typeof CHEB, to: typeof CHEB, units: number[]) => ({
    from_call: from,
    to_call: to,
    units,
  });

  it("merges neighbours running the same vehicles, in either order", () => {
    // R 619: one answer from Cheb, the same coaches reversed from Praha-Holešovice.
    const same = {
      units: [a, b, { ...b, observed_at: "later" }, { ...a, observed_at: "later" }],
      segments: [seg(CHEB, MOST, [0, 1]), seg(MOST, PRAHA, [2, 3])],
    } as unknown as Composition;
    expect(shownSegments(same)).toEqual([{ from_call: CHEB, to_call: PRAHA, members: [0, 1] }]);
  });

  it("keeps segments apart where the train changes", () => {
    expect(shownSegments(COMPOSITION).map((s) => s.members)).toEqual([[0], [1], [2]]);
  });
});

describe("callName", () => {
  it("names a span end past the terminus by the terminus", () => {
    const depot = { sequence: 9, location_id: "x", name: "Praha jih" };
    expect(callName(TRIP as never, depot)).toBe("Praha");
    expect(callName(TRIP as never, MOST)).toBe("Most");
  });
});
