import { describe, expect, it } from "vitest";
import {
  delayTone,
  delayWords,
  callDelay,
  segmentDays,
  segmentSummary,
  type HistoryCall,
  type HistoryDay,
  type TripHistory,
} from "./history";

function call(location: string, arrival: number | null, departure: number | null): HistoryCall {
  return {
    location_id: location,
    name: location,
    scheduled_arrival: null,
    scheduled_departure: null,
    actual_arrival: null,
    actual_departure: null,
    arrival_delay_s: arrival,
    departure_delay_s: departure,
    arrival_estimated: false,
    departure_estimated: false,
  };
}

function day(date: string, calls: HistoryCall[], cancelled = false): HistoryDay {
  return {
    service_date: date,
    trip_id: "t",
    cancelled,
    vehicles: [],
    worked_by: [],
    calls,
  };
}

const stop = (location_id: string) => ({
  location_id,
  name: location_id,
  runs: 1,
  median_delay_s: null,
  p90_delay_s: null,
  max_delay_s: null,
  on_time_share: null,
});

function history(days: HistoryDay[]): TripHistory {
  return {
    trip_id: "t",
    journey: { namespace: "cis:line_trip", key: "1:1" },
    since: "2026-10-10",
    until: "2026-10-12",
    cancelled: days.filter((d) => d.cancelled).length,
    days,
    stops: ["A", "B", "C", "D"].map(stop),
    generated_at: "2026-10-12T12:00:00Z",
    release_id: "r",
  };
}

const runs = history([
  // Early at the start (it does not matter to one boarding at B), late after.
  day("2026-10-12", [
    call("A", null, -180),
    call("B", 0, 60),
    call("C", 300, 360),
    call("D", 240, null),
  ]),
  day("2026-10-11", [
    call("A", null, 0),
    call("B", null, null),
    call("C", 60, 60),
    call("D", 0, null),
  ]),
  day("2026-10-10", [], true),
]);

describe("segmentDays", () => {
  it("measures each day from where the rider boards to where they get off", () => {
    const days = segmentDays(runs, 1, 3);
    const seconds = days.map((d) => [d.from, d.to, d.worst].map((x) => x.delayS));
    expect(days.map((d, i) => [d.day.service_date, ...seconds[i]!])).toEqual([
      ["2026-10-12", 60, 240, 360],
      ["2026-10-11", null, 0, 60],
      ["2026-10-10", null, null, null],
    ]);
    expect(days[0]!.calls.map((c) => c.location_id)).toEqual(["B", "C", "D"]);
  });

  it("takes a stop's arrival at the segment's end, else its departure", () => {
    expect(segmentDays(runs, 0, 2)[0]!.to.delayS).toBe(300);
  });

  it("carries whether a delay was inferred rather than seen", () => {
    const inferred = history([
      day("2026-10-12", [
        { ...call("A", null, 0), departure_estimated: true },
        call("B", 0, 60),
        { ...call("C", 300, null), arrival_estimated: true },
      ]),
    ]);
    const [run] = segmentDays(inferred, 0, 2);
    expect(run!.from).toEqual({ delayS: 0, estimated: true });
    expect(run!.to).toEqual({ delayS: 300, estimated: true });
    expect(run!.worst).toEqual({ delayS: 300, estimated: true });
    expect(callDelay(run!.calls[1]!, false)).toEqual({ delayS: 60, estimated: false });
  });
});

describe("segmentSummary", () => {
  it("counts measured runs that end on time, and their median at the end", () => {
    expect(segmentSummary(segmentDays(runs, 1, 3))).toEqual({ runs: 2, onTime: 0.5, median: 120 });
    expect(segmentSummary([])).toBeNull();
  });
});

describe("delays in words", () => {
  it("rounds to whole minutes, with the boards' tones", () => {
    expect([delayWords(20), delayWords(170), delayWords(-70)]).toEqual([
      "včas",
      "+3 min",
      "−1 min",
    ]);
    expect([delayTone(170), delayTone(700), delayTone(null)]).toEqual([
      "late",
      "veryLate",
      "unknown",
    ]);
  });
});
