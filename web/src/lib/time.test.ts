import {
  dayDividerLabel,
  dayKey,
  formatClock,
  pragueInstant,
  relativeDay,
  serviceDayOrigin,
} from "./time";

describe("time", () => {
  it("shows Prague wall time cut to the minute", () => {
    expect(formatClock("2026-10-21T12:29:59Z")).toBe("14:29");
    expect(formatClock("2026-10-21T12:30:01Z")).toBe("14:30");
    expect(formatClock("2026-12-01T13:05:00Z")).toBe("14:05");
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

  it("counts a service date from its noon less 12 h, so noon stays 12:00 when clocks change", () => {
    // 25 October 2026 has 25 hours: its noon is 12 hours after the origin.
    expect(Date.parse("2026-10-25T12:00:00+01:00") - serviceDayOrigin("2026-10-25")).toBe(
      12 * 3_600_000,
    );
  });
});
