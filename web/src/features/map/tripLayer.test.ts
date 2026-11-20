import { tripFeatures } from "./tripLayer";

describe("tripFeatures", () => {
  it("is the path and each stop, or nothing", () => {
    expect(tripFeatures(null).features).toEqual([]);
    const fc = tripFeatures({
      paths: [
        [
          [14, 50],
          [14.1, 50.1],
        ],
      ],
      stops: [{ id: "s1", position: [14, 50], name: "A", passed: true, rank: 1 }],
      colour: "#ff0000",
    });
    expect(fc.features.map((f) => f.properties?.kind)).toEqual(["path", "stop"]);
    expect(fc.features[1]!.properties).toMatchObject({ id: "s1", name: "A", rank: 1 });
  });
});
