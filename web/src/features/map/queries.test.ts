import type { Vehicle } from "../../api/client";
import { mergeVehicles } from "./queries";

const vehicle = (id: string, lastSeen: string, lon: number | null = 14.4): Vehicle => ({
  vehicle_id: id,
  feed: "jdf",
  source: "pid",
  state: "running",
  last_seen: lastSeen,
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

  it("leaves out vehicles without a position", () => {
    expect(mergeVehicles([[vehicle("x", "2026-10-22T14:00:00Z", null)]])).toEqual([]);
  });
});
