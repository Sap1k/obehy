import type { Alert } from "../api/client";
import { tripAlerts } from "./alerts";

type Target = Alert["informs"][number];

function alert(id: string, ...informs: Partial<Target>[]): Alert {
  return {
    alert_id: id,
    severity: "SEVERE",
    informs: informs.map((t) => ({
      kind: "stop",
      id: "",
      feed: "jdf",
      service_date: null,
      stop_place: null,
      ...t,
    })),
  } as Alert;
}

const run = {
  routeIds: ["jdf:route:1", "jdf:route:2"],
  tripIds: ["jdf:trip:1", "jdf:trip:2"],
  ahead: [
    { location_id: "jdf:stop:3", name: "Florenc" },
    { location_id: "jdf:stop:4", name: "Hlavní nádraží" },
  ],
};

describe("tripAlerts", () => {
  it("shows the run's line and trip alerts whatever their stops", () => {
    const shown = tripAlerts(
      [
        alert("line", { kind: "route", id: "jdf:route:1" }),
        alert("next-part", { kind: "trip", id: "jdf:trip:2" }),
      ],
      run,
    );
    expect(shown.map((s) => [s.alert.alert_id, s.stopName])).toEqual([
      ["line", null],
      ["next-part", null],
    ]);
  });

  it("shows a stop's alert only for a stop still ahead, named", () => {
    const shown = tripAlerts(
      [
        alert("ahead", { id: "jdf:stop:4" }),
        alert("passed-or-boarded", { id: "jdf:stop:1" }),
        alert("post-ahead", { id: "jdf:stop:3:post:2", stop_place: "jdf:stop:3" }),
      ],
      run,
    );
    expect(shown.map((s) => [s.alert.alert_id, s.stopName])).toEqual([
      ["ahead", "Hlavní nádraží"],
      ["post-ahead", "Florenc"],
    ]);
  });

  it("leaves out another line's alert at no stop ahead", () => {
    expect(tripAlerts([alert("other", { kind: "route", id: "jdf:route:9" })], run)).toEqual([]);
  });
});
