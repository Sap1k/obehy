import {
  dayDividerLabel,
  dayKey,
  formatClock,
  minutesBetween,
  pragueInstant,
  relativeDay,
} from "./time";

describe("time", () => {
  it("shows Prague wall time cut to the minute", () => {
    expect(formatClock("2026-10-21T12:29:59Z")).toBe("14:29");
    expect(formatClock("2026-10-21T12:30:01Z")).toBe("14:30");
    expect(formatClock("2026-12-01T13:05:00Z")).toBe("14:05");
  });

  it("counts minutes between the minutes shown", () => {
    // 14:29:59 → 14:30:01 is two seconds, but the shown minutes differ by one.
    expect(minutesBetween("2026-10-21T12:29:59Z", "2026-10-21T12:30:01Z")).toBe(1);
    expect(minutesBetween("2026-10-21T12:30:00Z", "2026-10-21T12:30:59Z")).toBe(0);
    expect(minutesBetween("2026-10-21T12:30:00Z", "2026-10-21T12:28:00Z")).toBe(-2);
  });

  it("keys days by the Prague date", () => {
    expect(dayKey("2026-10-21T22:30:00Z")).toBe("2026-10-22");
    expect(dayKey("2026-10-21T21:59:00Z")).toBe("2026-10-21");
  });

  it("names days relative to now", () => {
    const now = Date.parse("2026-10-21T12:00:00Z");
    expect(relativeDay("2026-10-21T20:00:00Z", now)).toBe("Dnes");
    expect(relativeDay("2026-10-21T22:07:00Z", now)).toBe("Zítra");
    expect(dayDividerLabel("2026-10-21T22:07:00Z", now)).toBe("Zítra · čtvrtek 22. října");
    expect(dayDividerLabel("2026-10-24T08:00:00Z", now)).toBe("Sobota 24. října");
  });

  it("turns Prague wall time into an instant across DST", () => {
    expect(pragueInstant("2026-10-21", "14:30").toISOString()).toBe("2026-10-21T12:30:00.000Z");
    expect(pragueInstant("2026-12-01", "14:30").toISOString()).toBe("2026-12-01T13:30:00.000Z");
    // 25 October 2026: clocks go back at 03:00 CEST; 04:00 is CET.
    expect(pragueInstant("2026-10-25", "04:00").toISOString()).toBe("2026-10-25T03:00:00.000Z");
  });
});
