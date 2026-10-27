import { ARROW_KEY } from "./badges";

// jsdom draws nothing: a 2D context whose drawing calls do nothing.
const noop = () => undefined;
const context = new Proxy({}, { get: () => noop, set: () => true });

/** A fresh badges module: the atlas is module state. */
async function fresh() {
  vi.resetModules();
  return import("./badges");
}

const pucks = (n: number, from = 0) =>
  Array.from({ length: n }, (_, i) => `L${from + i}|#123456|#ffffff|onTime`);

beforeEach(() => {
  vi.spyOn(HTMLCanvasElement.prototype, "getContext").mockReturnValue(
    context as CanvasRenderingContext2D,
  );
});

afterEach(() => {
  vi.restoreAllMocks();
});

describe("atlasWith", () => {
  it("returns the same atlas when it has every key", async () => {
    const { atlasWith } = await fresh();
    const atlas = atlasWith(pucks(3));
    expect(atlasWith(pucks(2))).toBe(atlas);
    expect(Object.keys(atlas.mapping).sort()).toEqual([...pucks(3), ARROW_KEY].sort());
  });

  it("stays within a texture every GPU takes, starting over when full", async () => {
    const { atlasWith } = await fresh();
    for (let i = 0; i < 3000; i += 500) {
      const atlas = atlasWith(pucks(500, i));
      expect(atlas.canvas.width).toBeLessThanOrEqual(4096);
      expect(atlas.canvas.height).toBeLessThanOrEqual(4096);
      for (const key of [...pucks(500, i), ARROW_KEY]) expect(atlas.mapping[key]).toBeDefined();
    }
    const restarted = atlasWith(pucks(2000, 10_000));
    expect(Object.keys(restarted.mapping).sort()).toEqual(
      [...pucks(2000, 10_000), ARROW_KEY].sort(),
    );
  });

  it("draws every icon on a grown atlas anew, never copying the old canvas", async () => {
    const drawImage = vi.fn();
    const fillText = vi.fn();
    const spy = new Proxy(
      {},
      {
        get: (_, k) => (k === "drawImage" ? drawImage : k === "fillText" ? fillText : noop),
        set: () => true,
      },
    );
    vi.mocked(HTMLCanvasElement.prototype.getContext).mockReturnValue(
      spy as CanvasRenderingContext2D,
    );
    const { atlasWith } = await fresh();
    atlasWith(pucks(3));
    fillText.mockClear();
    atlasWith(pucks(4));
    expect(drawImage).not.toHaveBeenCalled();
    expect(fillText).toHaveBeenCalledTimes(4);
  });

  it("starts over once its canvas loses its context, or the map is rebuilt", async () => {
    const { atlasWith, resetAtlas } = await fresh();
    const atlas = atlasWith(pucks(3));
    atlas.canvas.dispatchEvent(new Event("contextlost"));
    const redrawn = atlasWith(pucks(3));
    expect(redrawn).not.toBe(atlas);
    expect(redrawn.mapping).toEqual(atlas.mapping);
    resetAtlas();
    expect(atlasWith(pucks(3))).not.toBe(redrawn);
  });

  it("keeps the arrow and as many pucks as fit when asked for more", async () => {
    const { atlasWith } = await fresh();
    const atlas = atlasWith(pucks(5000));
    expect(atlas.canvas.height).toBeLessThanOrEqual(4096);
    expect(atlas.mapping[ARROW_KEY]).toBeDefined();
    expect(Object.keys(atlas.mapping).length).toBe(48 * 48);
  });
});
