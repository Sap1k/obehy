import type { Line, Trip, TripCall } from "../../api/client";
import { boardedCall, runCalls } from "./runCalls";

const call = (sequence: number, location_id: string, time: string): TripCall => ({
  sequence,
  location_id,
  name: location_id,
  latitude: null,
  longitude: null,
  boarding: "regular",
  alighting: "regular",
  flags: [],
  platform: null,
  scheduled_arrival: time,
  scheduled_departure: time,
  estimated_arrival: null,
  estimated_departure: null,
  status: "scheduled",
  source_class: null,
  delay_min: null,
  punctuality: "unknown",
  passed: false,
});

const line = (route_id: string, short_name: string): Line => ({
  route_id,
  short_name,
  long_name: null,
  mode: "rail",
  color: "1c1745",
  text_color: "ffffff",
});

const trip = (trip_id: string, l: Line, calls: TripCall[]) => ({ trip_id, line: l, calls }) as Trip;

// The run itself (later parts merged in, passed calls) is the API's (`include=run`, tested in
// tests/db/test_api.py); here only which call the rider opened the trip at.

describe("runCalls", () => {
  it("is the trip's own calls when no run was asked", () => {
    const one = trip("a", line("u1", "U1"), [call(1, "decin", "2026-10-22T12:32:00Z")]);
    expect(runCalls(one).map((c) => [c.trip_id, c.call.location_id])).toEqual([["a", "decin"]]);
  });
});

describe("boardedCall", () => {
  // A stop place served at three of its posts in a row; the first of them passed.
  const bus = trip("t", line("l", "100"), [
    { ...call(1, "a", "2026-10-22T12:00:00Z"), passed: true },
    { ...call(2, "s", "2026-10-22T12:02:00Z"), passed: true },
    call(3, "s", "2026-10-22T12:05:00Z"),
    call(4, "s", "2026-10-22T12:08:00Z"),
    call(5, "d", "2026-10-22T12:10:00Z"),
  ]);
  const calls = runCalls(bus);

  it("takes the board row's call by its sequence", () => {
    expect(boardedCall(calls, "t", "s", 2)?.call.sequence).toBe(2);
    expect(boardedCall(calls, "t", "s", 4)?.call.sequence).toBe(4);
  });

  it("without one, the first visit not yet passed, else the first", () => {
    expect(boardedCall(calls, "t", "s", undefined)?.call.sequence).toBe(3);
    const gone = runCalls({ ...bus, calls: bus.calls.map((c) => ({ ...c, passed: true })) });
    expect(boardedCall(gone, "t", "s", undefined)?.call.sequence).toBe(2);
    expect(boardedCall(calls, "other", "s", 4)?.call.sequence).toBe(3);
    expect(boardedCall(calls, "t", undefined, 3)).toBeUndefined();
  });
});
