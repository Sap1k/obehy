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

describe("runCalls", () => {
  it("appends the parts the train goes on as, marking the new line once", () => {
    const u1 = trip("a", line("u1", "U1"), [
      call(1, "decin", "2026-10-22T12:32:00Z"),
      call(2, "usti", "2026-10-22T13:12:00Z"),
    ]);
    const u4 = trip("b", line("u4", "U4"), [
      { ...call(1, "usti", "2026-10-22T13:15:00Z"), scheduled_arrival: null },
      call(2, "lovosice", "2026-10-22T13:31:00Z"),
    ]);
    const run = runCalls(u1, [u4]);
    expect(run.map((c) => c.call.location_id)).toEqual(["decin", "usti", "lovosice"]);
    // The shared stop keeps its arrival and takes the next part's departure.
    expect(run[1]!.call.scheduled_arrival).toBe("2026-10-22T13:12:00Z");
    expect(run[1]!.call.scheduled_departure).toBe("2026-10-22T13:15:00Z");
    expect(run[2]!.continuesAs?.short_name).toBe("U4");
  });

  it("stops at a part not loaded yet", () => {
    const u1 = trip("a", line("u1", "U1"), [call(1, "decin", "2026-10-22T12:32:00Z")]);
    expect(runCalls(u1, [undefined]).length).toBe(1);
  });
});

describe("boardedCall", () => {
  // A stop place served at three of its posts in a row.
  const bus = trip("t", line("l", "100"), [
    call(1, "a", "2026-10-22T12:00:00Z"),
    call(2, "s", "2026-10-22T12:02:00Z"),
    call(3, "s", "2026-10-22T12:05:00Z"),
    call(4, "s", "2026-10-22T12:08:00Z"),
    call(5, "d", "2026-10-22T12:10:00Z"),
  ]);
  const calls = runCalls(bus, []);
  const at = (now: string) => Date.parse(now);

  it("takes the board row's call by its sequence", () => {
    expect(boardedCall(calls, "t", "s", 3, at("2026-10-22T11:00:00Z"))?.call.sequence).toBe(3);
    expect(boardedCall(calls, "t", "s", 4, at("2026-10-22T11:00:00Z"))?.call.sequence).toBe(4);
  });

  it("without one, the first visit not yet passed, else the first", () => {
    expect(boardedCall(calls, "t", "s", undefined, at("2026-10-22T12:04:00Z"))?.call.sequence).toBe(
      3,
    );
    expect(boardedCall(calls, "t", "s", undefined, at("2026-10-22T13:00:00Z"))?.call.sequence).toBe(
      2,
    );
    expect(boardedCall(calls, "other", "s", 4, at("2026-10-22T11:00:00Z"))?.call.sequence).toBe(2);
    expect(boardedCall(calls, "t", undefined, 3, at("2026-10-22T11:00:00Z"))).toBeUndefined();
  });
});
