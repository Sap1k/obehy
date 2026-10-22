import { freshnessOf } from "./freshness";

describe("freshnessOf", () => {
  const now = Date.parse("2026-10-22T20:00:00Z");
  const at = (secondsAgo: number) => new Date(now - secondsAgo * 1000).toISOString();

  it("is live while some source reported in the last two minutes", () => {
    expect(freshnessOf({ failing: false, online: true, realtimeAt: at(25), now })).toBe("live");
  });

  it("is delayed when every source is quiet, or nothing is known", () => {
    expect(freshnessOf({ failing: false, online: true, realtimeAt: at(180), now })).toBe("delayed");
    expect(freshnessOf({ failing: false, online: true, realtimeAt: null, now })).toBe("delayed");
  });

  it("is offline when requests fail or the device is offline", () => {
    expect(freshnessOf({ failing: true, online: true, realtimeAt: at(5), now })).toBe("offline");
    expect(freshnessOf({ failing: false, online: false, realtimeAt: at(5), now })).toBe("offline");
  });
});
