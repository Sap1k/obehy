import { AREA, cellsFor, quarters } from "./grid";

describe("cellsFor", () => {
  it("covers a city with the whole-degree cells it touches", () => {
    const prague = { west: 14.2, south: 49.9, east: 14.8, north: 50.2 };
    expect(cellsFor(prague).map((c) => c.key)).toEqual(["14,49,15,50", "14,50,15,51"]);
  });

  it("covers the whole area in at most 36 cells, clamped to it", () => {
    const cells = cellsFor({ west: 0, south: 40, east: 30, north: 60 });
    expect(cells.length).toBe(9 * 4);
    expect(cells[0]!.key).toBe("11,48,12,49");
    expect(cells.at(-1)!.key).toBe("19,51,20,52");
    expect(cellsFor({ west: 0, south: 0, east: 5, north: 5 })).toEqual([]);
    expect(AREA.west).toBeLessThan(AREA.east);
  });

  it("aligns edges, so a viewport ending on a degree takes no extra cell", () => {
    expect(cellsFor({ west: 14, south: 50, east: 15, north: 51 }).map((c) => c.key)).toEqual([
      "14,50,15,51",
    ]);
  });

  it("replaces a split cell by the quarters inside the view, printed exactly", () => {
    const split = new Set(["14,50,15,51", "14,50,14.5,50.5"]);
    const keys = cellsFor({ west: 14.1, south: 50.1, east: 14.4, north: 50.4 }, split).map(
      (c) => c.key,
    );
    expect(keys).toEqual([
      "14,50,14.25,50.25",
      "14.25,50,14.5,50.25",
      "14,50.25,14.25,50.5",
      "14.25,50.25,14.5,50.5",
    ]);
  });

  it("stops splitting at a quarter degree", () => {
    const [smallest] = cellsFor(
      { west: 14.1, south: 50.1, east: 14.2, north: 50.2 },
      new Set(["14,50,15,51", "14,50,14.5,50.5"]),
    );
    expect(quarters(smallest!)).toEqual([]);
  });
});
