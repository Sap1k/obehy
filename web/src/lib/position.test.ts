import { distanceText, metresBetween, roughly } from "./position";

describe("position", () => {
  const here = { latitude: 50.0755, longitude: 14.4378, at: 0 };

  it("leaves the browser only rounded to about a kilometre", () => {
    expect(roughly(here)).toBe("50.08,14.44");
  });

  it("measures and words distances", () => {
    expect(Math.round(metresBetween(here, 50.0845, 14.4378))).toBe(1001);
    expect(distanceText(342)).toBe("340 m");
    expect(distanceText(3)).toBe("10 m");
    expect(distanceText(2449)).toBe("2,4 km");
    expect(distanceText(18_400)).toBe("18 km");
  });
});
