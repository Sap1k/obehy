import { describe, expect, it } from "vitest";
import { delayWords } from "./history";

// The segment delays, their minutes and bands and the summary are measured by the API
// (`api/segments.py`, tested in `tests/unit/test_api_segments.py`); here only how they read.

describe("delays in words", () => {
  it("words the API's shown minutes", () => {
    expect([delayWords(0), delayWords(2), delayWords(-2)]).toEqual(["včas", "+2 min", "−2 min"]);
  });
});
