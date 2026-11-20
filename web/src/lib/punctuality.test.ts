import { describe, expect, it } from "vitest";
import { timeState, toneOf } from "./punctuality";

// The delay and its band are the API's (`api/punctuality.py`, `tests/unit/test_api_punctuality.py`);
// here only how they read.

const at = (min: number) => new Date(Date.UTC(2026, 9, 21, 12, 30) + min * 60_000).toISOString();

describe("punctuality", () => {
  it.each([
    ["early", "early"],
    ["on_time", "onTime"],
    ["late", "late"],
    ["very_late", "veryLate"],
    ["unknown", "unknown"],
    ["cancelled", "cancelled"],
  ] as const)("the API's %s is coloured %s", (band, tone) => {
    expect(toneOf(band)).toBe(tone);
  });

  it("words the delay", () => {
    expect(timeState(at(0), at(0), 0, "on_time").caption).toBe("Včas");
    expect(timeState(at(0), at(4), 4, "late")).toMatchObject({ caption: "+4 min", tone: "late" });
    expect(timeState(at(0), at(-2), -2, "early").caption).toBe("−2 min");
  });

  it("strikes a cancelled call", () => {
    const s = timeState(at(0), null, null, "cancelled");
    expect(s).toMatchObject({ tone: "cancelled", caption: "Zrušeno", struck: true });
  });

  it("shows the timetable without realtime", () => {
    expect(timeState(at(0), null, null, "unknown")).toMatchObject({
      tone: "unknown",
      caption: "Dle JŘ",
      shown: at(0),
    });
  });

  it("marks stale or offline data as approximate and neutral", () => {
    expect(timeState(at(0), at(4), 4, "unknown")).toMatchObject({
      tone: "unknown",
      caption: "≈ +4 min",
      shown: at(4),
    });
    expect(timeState(at(0), at(0), 0, "on_time", true).caption).toBe("≈ včas");
  });
});
