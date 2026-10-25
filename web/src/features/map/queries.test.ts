import type { Vehicle } from "../../api/client";
import { mergeVehicles, rowAmong } from "./queries";

const vehicle = (
  id: string,
  lastSeen: string,
  lon: number | null = 14.4,
  positionAt: string | null = null,
): Vehicle => ({
  vehicle_id: id,
  feed: "jdf",
  source: "pid",
  state: "running",
  last_seen: lastSeen,
  position_at: positionAt,
  latitude: lon === null ? null : 50.08,
  longitude: lon,
  bearing: null,
  trip_id: null,
  service_date: null,
  line: null,
  headsign: null,
  delay_s: null,
});

describe("mergeVehicles", () => {
  it("keeps one row per vehicle across cells, the fresher one", () => {
    const merged = mergeVehicles([
      [vehicle("a", "2026-10-22T14:00:00Z", 15.0), vehicle("b", "2026-10-22T14:00:00Z")],
      [vehicle("a", "2026-10-22T14:00:10Z", 15.001)],
      undefined,
    ]);
    expect(merged.map((v) => [v.vehicle_id, v.longitude])).toEqual([
      ["a", 15.001],
      ["b", 14.4],
    ]);
  });

  it("keeps the newer fix, not the newer report", () => {
    // The SŽ map's report is heard every poll; its position changes seldom.
    const merged = mergeVehicles([
      [vehicle("t", "2026-10-22T14:00:30Z", 15.0, "2026-10-22T13:58:00Z")],
      [vehicle("t", "2026-10-22T14:00:10Z", 15.01, "2026-10-22T14:00:05Z")],
    ]);
    expect(merged.map((v) => v.longitude)).toEqual([15.01]);
  });

  it("never draws a held vehicle back to an older fix", () => {
    const held = mergeVehicles([[vehicle("a", "2026-10-22T14:00:10Z", 15.01)]]);
    // A stale copy of the cell, or its cached answer after a pan, comes in after the newer one.
    const again = mergeVehicles(
      [[{ ...vehicle("a", "2026-10-22T14:00:00Z", 15.0), delay_s: 60 }]],
      held,
    );
    expect(again.map((v) => [v.longitude, v.delay_s])).toEqual([[15.01, 60]]);
    const next = mergeVehicles([[vehicle("a", "2026-10-22T14:00:20Z", 15.02)]], again);
    expect(next.map((v) => v.longitude)).toEqual([15.02]);
  });

  it("drops a held vehicle no answer has any more", () => {
    const held = mergeVehicles([[vehicle("a", "2026-10-22T14:00:10Z")]]);
    expect(
      mergeVehicles([[vehicle("b", "2026-10-22T14:00:10Z")]], held).map((v) => v.vehicle_id),
    ).toEqual(["b"]);
  });

  it("leaves out vehicles without a position", () => {
    expect(mergeVehicles([[vehicle("x", "2026-10-22T14:00:00Z", null)]])).toEqual([]);
  });
});

describe("rowAmong", () => {
  it("finds a vehicle's freshest row among the cells, or none", () => {
    const answers = [
      { generated_at: "", vehicles: [vehicle("a", "2026-10-22T14:00:00Z", 15.0)] },
      undefined,
      { generated_at: "", vehicles: [vehicle("a", "2026-10-22T14:00:10Z", 15.001)] },
    ];
    expect(rowAmong(answers, "a")?.longitude).toBe(15.001);
    expect(rowAmong(answers, "b")).toBeUndefined();
  });
});
