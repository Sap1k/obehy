import { GLIDE_MS, drawnPositions, positionsAt, progress, startGlide } from "./motion";

describe("glides", () => {
  const drawn = new Map([
    ["a", [14.4, 50.08] as const],
    ["gone", [14.5, 50.1] as const],
    ["far", [14.0, 50.0] as const],
  ]);

  it("moves each vehicle from where it was drawn, by its ID", () => {
    const g = startGlide(
      drawn,
      [
        { id: "new", position: [14.45, 50.09] },
        { id: "a", position: [14.41, 50.09] },
      ],
      1000,
    );
    const out = positionsAt(g, 0.5, new Float64Array(4));
    // A new vehicle appears in place, even in the slot order of another one.
    expect([out[0], out[1]]).toEqual([14.45, 50.09]);
    expect(out[2]).toBeCloseTo(14.405);
    expect(out[3]).toBeCloseTo(50.085);
  });

  it("does not glide a jump", () => {
    const g = startGlide(drawn, [{ id: "far", position: [14.4, 50.0] }], 0);
    expect(Array.from(positionsAt(g, 0.1, new Float64Array(2)))).toEqual([14.4, 50.0]);
  });

  it("runs linearly for one second and hands the drawn positions on", () => {
    const g = startGlide(drawn, [{ id: "a", position: [14.42, 50.08] }], 1000);
    expect(progress(g, 1000 + GLIDE_MS / 4)).toBe(0.25);
    expect(progress(g, 1000 + GLIDE_MS * 2)).toBe(1);
    const at = positionsAt(g, 0.25, new Float64Array(2));
    expect(drawnPositions(g, at).get("a")![0]).toBeCloseTo(14.405);
  });
});
