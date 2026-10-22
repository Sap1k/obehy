import { anchorIndex, collapse } from "./timetable";

describe("collapsed timetable", () => {
  it("shows six calls or fewer in full", () => {
    expect(collapse(6, 0)).toBeNull();
  });

  it("shows the anchor, two after it, an expander and the last call", () => {
    expect(collapse(11, 0)).toEqual({ before: null, head: [0, 1, 2], middle: [3, 9], last: 10 });
  });

  it("hides calls before the anchor", () => {
    expect(collapse(12, 4)).toEqual({ before: [0, 3], head: [4, 5, 6], middle: [7, 10], last: 11 });
  });

  it("does not hide a single call", () => {
    expect(collapse(7, 2)).toEqual({ before: [0, 1], head: [2, 3, 4, 5], middle: null, last: 6 });
  });

  it("near the end shows what is left", () => {
    expect(collapse(8, 6)).toEqual({ before: [0, 5], head: [6, 7], middle: null, last: null });
  });

  it("opens each hidden range on its own", () => {
    expect(collapse(12, 4, { before: true, middle: false })).toEqual({
      before: null,
      head: [0, 1, 2, 3, 4, 5, 6],
      middle: [7, 10],
      last: 11,
    });
    expect(collapse(12, 4, { before: false, middle: true })).toEqual({
      before: [0, 3],
      head: [4, 5, 6, 7, 8, 9, 10],
      middle: null,
      last: 11,
    });
    expect(collapse(12, 4, { before: true, middle: true })).toBeNull();
    // Nothing left hidden: every call shows.
    expect(collapse(11, 0, { before: false, middle: true })).toBeNull();
  });

  it("anchors on the boarding stop, else the next call", () => {
    const calls = [{ location_id: "a" }, { location_id: "b" }, { location_id: "c" }];
    expect(anchorIndex(calls, [true, false, false], "c")).toBe(2);
    expect(anchorIndex(calls, [true, false, false])).toBe(1);
    expect(anchorIndex(calls, [true, true, true])).toBe(2);
  });
});
