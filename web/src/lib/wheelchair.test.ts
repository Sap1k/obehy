import { stepFree } from "./wheelchair";

const traits = (low_floor: boolean | null, wheelchair_accessible: boolean | null) => ({
  low_floor,
  wheelchair_accessible,
});

describe("stepFree", () => {
  it("trusts the timetable while no vehicle is known", () => {
    expect(stepFree("accessible", null)).toBe("accessible");
    expect(stepFree("booking_recommended", undefined)).toBe("accessible");
    expect(stepFree("booking_required", traits(null, null))).toBe("booking");
    expect(stepFree("partly", null)).toBe("partly");
    expect(stepFree(null, null)).toBeNull();
  });

  it("lets a known vehicle confirm or overrule it", () => {
    expect(stepFree("accessible", traits(false, false))).toBeNull();
    expect(stepFree(null, traits(true, null))).toBe("accessible");
    expect(stepFree("partly", traits(null, true))).toBe("accessible");
    // The carrier still wants the booking, whatever runs.
    expect(stepFree("booking_required", traits(true, true))).toBe("booking");
  });
});
