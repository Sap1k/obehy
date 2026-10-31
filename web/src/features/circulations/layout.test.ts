import { describe, expect, it } from "vitest";
import type { ChainJourney, ScopedChain, ScopeRoute } from "../../api/client";
import {
  hourLabel,
  linesMatching,
  hourTicks,
  percent,
  rowsOf,
  shortRoute,
  spanOf,
  tickStep,
  vehicleModel,
  vehicleName,
  windowOf,
  zoomAround,
  zoomed,
} from "./layout";

function journey(
  tripId: string | null,
  departure: string | null,
  arrival: string | null,
  seen: [string, string],
): ChainJourney {
  return {
    journey: { namespace: "cis:line_trip", key: tripId ?? "x" },
    service_date: "2026-10-06",
    trip_id: tripId,
    line: null,
    route_name: null,
    headsign: null,
    origin: null,
    departure,
    destination: null,
    arrival,
    first_seen: seen[0],
    last_seen: seen[1],
    layover_s: null,
  };
}

function chain(id: string, journeys: ChainJourney[], matched: number[]): ScopedChain {
  return {
    vehicle_id: `duk:${id}`,
    source: "duk",
    source_vehicle_id: id,
    fleet_number: null,
    public_label: null,
    manufacturer: null,
    model: null,
    first_seen: journeys[0]!.first_seen,
    last_seen: journeys.at(-1)!.last_seen,
    journeys,
    matched,
  };
}

// 2026-10-06 is in summer time: Prague is UTC+2.
const late = chain(
  "1001",
  [
    journey("t1", "2026-10-06T21:00:00+02:00", "2026-10-06T21:30:00+02:00", [
      "2026-10-06T20:58:00+02:00",
      "2026-10-06T21:31:00+02:00",
    ]),
    // Past midnight, with no schedule in history: where it was seen.
    journey(null, null, null, ["2026-10-07T00:10:00+02:00", "2026-10-07T00:40:00+02:00"]),
  ],
  [0],
);
const early = chain(
  "1002",
  [
    journey("t2", "2026-10-06T05:00:00+02:00", "2026-10-06T05:20:00+02:00", [
      "2026-10-06T04:59:00+02:00",
      "2026-10-06T05:21:00+02:00",
    ]),
  ],
  [0],
);

describe("rowsOf", () => {
  it("lays bars out from the service date's midnight, past 24:00", () => {
    const [row] = rowsOf([late], "2026-10-06", {});
    expect(row!.bars.map((b) => [b.start, b.end, b.inScope])).toEqual([
      [21 * 3600, 21.5 * 3600, true],
      [24 * 3600 + 600, 24 * 3600 + 2400, false],
    ]);
  });

  it("orders rows by vehicle ID, numbers as numbers, and filters by vehicle", () => {
    const nine = { ...early, vehicle_id: "duk:999", source_vehicle_id: "999" };
    expect(
      rowsOf([early, late, nine], "2026-10-06", {}).map((r) => r.chain.source_vehicle_id),
    ).toEqual(["999", "1001", "1002"]);
    expect(rowsOf([late, early], "2026-10-06", { vehicle: "DUK:1001" })).toHaveLength(1);
  });

  it("puts a vehicle's chains in one row, the break between them kept", () => {
    const evening = {
      ...early,
      journeys: [
        journey("t3", "2026-10-06T15:00:00+02:00", "2026-10-06T15:40:00+02:00", [
          "2026-10-06T14:58:00+02:00",
          "2026-10-06T15:41:00+02:00",
        ]),
      ],
      first_seen: "2026-10-06T14:58:00+02:00",
      last_seen: "2026-10-06T15:41:00+02:00",
    };
    const [row, ...others] = rowsOf([evening, early], "2026-10-06", {});
    expect(others).toEqual([]);
    expect(row!.key).toBe("duk:1002@2026-10-06");
    expect(row!.chains).toEqual([early, evening]);
    expect(row!.bars.map((b) => [b.start, b.part])).toEqual([
      [5 * 3600, 0],
      [15 * 3600, 1],
    ]);
    expect(row!.breaks).toEqual([{ start: 5 * 3600 + 1200, end: 15 * 3600 }]);
  });

  it("marks the trip the page was opened from", () => {
    const [row] = rowsOf([late], "2026-10-06", { tripId: "t1" });
    expect(row!.bars.map((b) => b.highlighted)).toEqual([true, false]);
  });
});

describe("the axis", () => {
  it("spans whole hours over every row", () => {
    const span = spanOf(rowsOf([late, early], "2026-10-06", {}))!;
    expect(span).toEqual({ start: 5 * 3600, end: 25 * 3600 });
    expect(hourTicks(span).slice(-3).map(hourLabel)).toEqual(["23", "0", "1"]);
    expect(percent(15 * 3600, span)).toBe(50);
  });

  it("labels fewer hours when they are narrow", () => {
    const span = { start: 4 * 3600, end: 26 * 3600 };
    expect(tickStep(160)).toBe(1);
    expect(hourTicks(span, tickStep(20))).toEqual([4, 6, 8, 10, 12, 14, 16, 18, 20, 22, 24, 26]);
  });

  it("has none without bars", () => {
    expect(spanOf([])).toBeNull();
  });
});

describe("the vehicle column", () => {
  const known = {
    ...late,
    source_vehicle_id: "3:9753",
    fleet_number: "9753",
    manufacturer: "Škoda",
    model: "15T",
  };

  it("names a vehicle by its fleet number, and finds it by it", () => {
    expect([vehicleName(known), vehicleModel(known)]).toEqual(["9753", "Škoda 15T"]);
    expect([vehicleName(early), vehicleModel(early)]).toEqual(["1002", null]);
    expect(rowsOf([known, early], "2026-10-06", { vehicle: "9753" })).toHaveLength(1);
  });
});

describe("zoom", () => {
  it("doubles and halves within bounds, never narrower than the fit", () => {
    expect(zoomed(160, 2, 40)).toBe(320);
    expect(zoomed(640, 2, 40)).toBe(640);
    expect(zoomed(80, 0.25, 40)).toBe(40);
    expect(zoomed(80, 0.01, 1)).toBe(6);
  });

  it("keeps the time under the pointer where it was", () => {
    // 3 h into the lane at 160 px is 480 px: scrolled 300, the pointer 132 + 180 px in.
    const left = zoomAround(300, 312, 132, 160, 320);
    expect(left + 180).toBe(3 * 320);
    expect(zoomAround(0, 140, 132, 160, 80)).toBe(0);
  });
});

describe("linesMatching", () => {
  const route = (short_name: string, long_name: string): ScopeRoute => ({
    line: {
      route_id: short_name,
      short_name,
      long_name,
      mode: "bus",
      color: null,
      text_color: null,
    },
    agency: null,
    vehicles: 1,
  });
  const day = [
    route("9", "Sídliště Spořilov - Sídliště Řepy"),
    route("119", "Nádraží Veleslavín - Letiště"),
    route("91", "Lehovec - Sídliště Barrandov"),
  ];
  const names = (q: string) => linesMatching(day, q).map((r) => r.line.short_name);

  it("puts the number itself first, then numbers starting so, in the day's order", () => {
    expect(names("9")).toEqual(["9", "91", "119"]);
  });

  it("matches names regardless of case and diacritics, words in order", () => {
    expect(names("sidliste repy")).toEqual(["9"]);
    expect(names("repy sidliste")).toEqual([]);
    expect(names("LETISTE")).toEqual(["119"]);
    expect(names(" - ")).toEqual([]);
  });
});

describe("windowOf", () => {
  const sizes = { rowPx: 44, headPx: 24, spare: 2 };

  it("draws the rows in view and a few either side", () => {
    expect(windowOf(0, 440, 1000, sizes)).toEqual({ first: 0, last: 12 });
    expect(windowOf(24 + 44 * 100, 440, 1000, sizes)).toEqual({ first: 98, last: 112 });
  });

  it("keeps within the rows", () => {
    expect(windowOf(24 + 44 * 995, 440, 1000, sizes)).toEqual({ first: 993, last: 1000 });
    expect(windowOf(0, 440, 0, sizes)).toEqual({ first: 0, last: 0 });
  });
});

describe("shortRoute", () => {
  it("leaves out the town within one, keeps only the towns between two", () => {
    expect(shortRoute("Praha,Na Knížecí", "Praha,Olšanské hřbitovy")).toBe(
      "Na Knížecí → Olšanské hřbitovy",
    );
    expect(shortRoute("Chlumec,,nám.", "Ústí n.L.,,aut.nádr.")).toBe("Chlumec → Ústí n.L.");
    expect(shortRoute("Teplice,Benešovo nám.", "Teplice")).toBe("Benešovo nám. → Teplice");
  });
});
