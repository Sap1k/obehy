import { timeState, toneOf } from "./punctuality";

const at = (min: number) => new Date(Date.UTC(2026, 9, 21, 12, 30) + min * 60_000).toISOString();

describe("punctuality", () => {
  it.each([
    [-3, "early"],
    [-2, "early"],
    [-1, "onTime"],
    [0, "onTime"],
    [2, "onTime"],
    [3, "late"],
    [9, "late"],
    [10, "veryLate"],
  ] as const)("a delay of %i min is %s", (delay, tone) => {
    expect(toneOf(delay)).toBe(tone);
    expect(timeState(at(0), at(delay), "predicted", false).tone).toBe(tone);
  });

  it("words the delay", () => {
    expect(timeState(at(0), at(0), "predicted", false).caption).toBe("Včas");
    expect(timeState(at(0), at(4), "actual", false).caption).toBe("+4 min");
    expect(timeState(at(0), at(-2), "inferred", false).caption).toBe("−2 min");
  });

  it("strikes a cancelled call", () => {
    const s = timeState(at(0), null, "cancelled", null);
    expect(s).toMatchObject({ tone: "cancelled", caption: "Zrušeno", struck: true });
  });

  it("shows the timetable without realtime", () => {
    expect(timeState(at(0), null, "scheduled", null)).toMatchObject({
      tone: "unknown",
      caption: "Dle JŘ",
      shown: at(0),
    });
    expect(timeState(at(0), at(3), "no_realtime", null).caption).toBe("Dle JŘ");
  });

  it("marks stale data as approximate and neutral", () => {
    expect(timeState(at(0), at(4), "predicted", true)).toMatchObject({
      tone: "unknown",
      caption: "≈ +4 min",
    });
    expect(timeState(at(0), at(0), "predicted", true).caption).toBe("≈ včas");
  });
});
