import type { Line, Trip, TripCall } from "../../api/client";
import { runCalls } from "./runCalls";

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
