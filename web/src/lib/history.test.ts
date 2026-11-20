import { describe, expect, it } from "vitest";
import { delayTone, delayWords } from "./history";

// The segment delays and the summary are measured by the API (`api/segments.py`, tested in
// `tests/unit/test_api_segments.py`); here only how they read.

describe("delays in words", () => {
  it("rounds to whole minutes, with the boards' tones", () => {
    expect([delayWords(20), delayWords(170), delayWords(-70)]).toEqual([
      "včas",
      "+3 min",
      "−1 min",
    ]);
    expect([delayTone(170), delayTone(700), delayTone(null)]).toEqual([
      "late",
      "veryLate",
      "unknown",
    ]);
  });
});
