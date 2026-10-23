import { sheetHeights } from "./sheet";

describe("sheetHeights", () => {
  it("is the header, 45 % and 70 % of the map", () => {
    expect(sheetHeights(700)).toEqual({ peek: 148, half: 315, full: 490 });
  });

  it("never goes below the header, nor above full, on a short map", () => {
    expect(sheetHeights(180)).toEqual({ peek: 148, half: 148, full: 148 });
    expect(sheetHeights(0)).toEqual({ peek: 148, half: 148, full: 148 });
  });
});
