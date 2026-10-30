import { describe, expect, it } from "vitest";
import { delayScale, delayWords, minuteTicks, place, rangeBar, type HistoryStop } from "./history";

function stop(median: number | null, p90: number | null, worst: number | null): HistoryStop {
  return {
    location_id: "s",
    name: "Zastávka",
    runs: median === null ? 0 : 10,
    on_time_share: median === null ? null : 0.8,
    median_delay_s: median,
    p90_delay_s: p90,
    max_delay_s: worst,
  };
}

describe("delayScale", () => {
  it("reaches from a minute early to five minutes at least", () => {
    expect(delayScale([stop(30, 90, 120)])).toEqual({ min: -60, max: 300 });
    expect(delayScale([stop(null, null, null)])).toEqual({ min: -60, max: 300 });
  });

  it("widens to the 90th percentile but not to the worst run", () => {
    expect(delayScale([stop(60, 430, 3000), stop(-150, 0, 30)])).toEqual({
      min: -180,
      max: 480,
    });
  });
});

describe("rangeBar", () => {
  const scale = { min: -60, max: 540 };

  it("places the median, the 90th percentile and the worst run", () => {
    expect(rangeBar(stop(120, 240, 420), scale)).toEqual({
      zero: 10,
      median: 30,
      p90: 50,
      worst: 80,
      beyond: false,
      tone: "onTime",
    });
  });

  it("holds a worst run past the scale at its end, marked", () => {
    const bar = rangeBar(stop(300, 480, 1800), scale)!;
    expect([bar.worst, bar.beyond, bar.tone]).toEqual([100, true, "late"]);
  });

  it("has none where no run has a time", () => {
    expect(rangeBar(stop(null, null, null), scale)).toBeNull();
  });
});

describe("scale marks and words", () => {
  it("marks minutes, coarser on a wider scale", () => {
    expect(minuteTicks({ min: -60, max: 300 })).toEqual([-1, 0, 1, 2, 3, 4, 5]);
    expect(minuteTicks({ min: -60, max: 1200 })).toEqual([0, 5, 10, 15, 20]);
  });

  it("holds positions to the scale", () => {
    expect(place(-600, { min: -60, max: 300 })).toBe(0);
  });

  it("words a delay in whole minutes", () => {
    expect([delayWords(20), delayWords(200), delayWords(-100)]).toEqual([
      "včas",
      "+3 min",
      "−2 min",
    ]);
  });
});
