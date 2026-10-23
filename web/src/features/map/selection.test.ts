import { openedFrom } from "./selection";

describe("openedFrom", () => {
  it("reads each route", () => {
    expect(openedFrom({})).toEqual({ key: "", kind: "none" });
    expect(openedFrom({ stopId: "S1" })).toEqual({ key: "stop:S1", kind: "stop", stopId: "S1" });
    expect(openedFrom({ stopId: "S1", tripId: "T1", date: "2026-10-22" })).toEqual({
      key: "trip:T1/2026-10-22",
      kind: "trip",
      stopId: "S1",
      trip: { tripId: "T1", date: "2026-10-22" },
    });
    expect(openedFrom({ vehicleId: "sz-mapa:TR/1154" })).toEqual({
      key: "vehicle:sz-mapa:TR/1154",
      kind: "vehicle",
      vehicleId: "sz-mapa:TR/1154",
    });
  });

  it("names a trip by itself, whichever board it is opened from", () => {
    const a = openedFrom({ stopId: "S1", tripId: "T1", date: "2026-10-22" });
    const b = openedFrom({ stopId: "S2", tripId: "T1", date: "2026-10-22" });
    expect(a.key).toBe(b.key);
  });
});
