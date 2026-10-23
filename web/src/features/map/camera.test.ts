import {
  afterGesture,
  focusOf,
  initialCamera,
  planCamera,
  type CameraInput,
  type CameraState,
  type CameraView,
  type Focus,
  type Insets,
  type LngLat,
} from "./camera";

const PRAHA: LngLat = [14.42, 50.08];
const OSTRAVA: LngLat = [18.29, 49.84];
const PANEL: Insets = { left: 470, bottom: 0 };
const TWO_PANELS: Insets = { left: 882, bottom: 0 };

/** A map showing Praha: points east of 16° are off screen; a point counts as covered when it
 * is off screen or the panels reach past it (Praha sits 600 px from the left). */
function view(over: Partial<CameraView> = {}): CameraView {
  return {
    moving: false,
    flying: false,
    zoom: 13,
    covered: (at, insets) => at[0] > 16 || insets.left >= 600,
    ...over,
  };
}

const focus = (key: string, at: LngLat | null, ready = true): Focus => ({
  key,
  ready,
  at,
  zoomIn: false,
});

function input(over: Partial<CameraInput> = {}): CameraInput {
  return { focus: focus("", null), selected: null, follow: null, insets: PANEL, ...over };
}

/** Plan a sequence of renders, returning each move's kind (or null). */
function run(steps: [CameraInput, CameraView?][], start: CameraState = initialCamera()) {
  let state = { ...start, insets: PANEL };
  return steps.map(([i, v]) => {
    const r = planCamera(state, i, v ?? view());
    state = r.state;
    return r.move?.kind ?? null;
  });
}

describe("planCamera", () => {
  it("flies once between far-apart trips, not while the second one loads", () => {
    const moves = run([
      [input({ focus: focus("trip:a", OSTRAVA), selected: OSTRAVA })],
      [input({ focus: focus("trip:a", OSTRAVA), selected: OSTRAVA }), view({ flying: true })],
      // Trip b opened: unknown while it loads.
      [input({ focus: focus("trip:b", null, false) })],
      [input({ focus: focus("trip:b", null, false) })],
      [input({ focus: focus("trip:b", [17, 49.5]), selected: [17, 49.5] })],
      [input({ focus: focus("trip:b", [17, 49.5]), selected: [17, 49.5] })],
    ]);
    expect(moves).toEqual(["fly", null, null, null, "fly", null]);
  });

  it("does not move for a vehicle already in view", () => {
    expect(run([[input({ focus: focus("vehicle:x", PRAHA), selected: PRAHA })]])).toEqual([null]);
  });

  it("flies to a new navigation during a flight to the one before", () => {
    const moves = run([
      [input({ focus: focus("vehicle:x", OSTRAVA) })],
      [input({ focus: focus("vehicle:y", PRAHA) }), view({ flying: true, moving: true })],
    ]);
    expect(moves).toEqual(["fly", "fly"]);
  });

  it("takes panels opened during a flight once the map stops", () => {
    const flying = view({ flying: true, moving: true });
    const moves = run([
      [input({ focus: focus("vehicle:x", OSTRAVA) })],
      [input({ focus: focus("vehicle:x", OSTRAVA), insets: { left: 0, bottom: 0 } }), flying],
      [input({ focus: focus("vehicle:x", OSTRAVA), insets: { left: 0, bottom: 0 } })],
    ]);
    expect(moves).toEqual(["fly", null, "pad"]);
  });

  it("keeps the map still when a panel opens, unless it covers the vehicle", () => {
    const shown = { ...initialCamera(), shownKey: "trip:a" };
    const still = run([[input({ focus: focus("trip:a", PRAHA), selected: [14, 50] })]], shown);
    expect(still).toEqual([null]);
    const opened = run(
      [[input({ focus: focus("trip:a", PRAHA), selected: PRAHA, insets: TWO_PANELS })]],
      shown,
    );
    expect(opened).toEqual(["ease"]);
    const nothing = run(
      [[input({ focus: focus("trip:a", null), insets: { left: 0, bottom: 0 } })]],
      shown,
    );
    expect(nothing).toEqual(["pad"]);
  });

  it("follows each new position, but not during our flight", () => {
    const shown = { ...initialCamera(), shownKey: "vehicle:x" };
    const f = (at: LngLat, v?: CameraView): [CameraInput, CameraView?] => [
      input({ focus: focus("vehicle:x", at), selected: at, follow: at }),
      v,
    ];
    const moves = run(
      [
        f(PRAHA),
        f(PRAHA),
        f([14.43, 50.08], view({ flying: true })),
        f([14.43, 50.08]),
        f([14.44, 50.08]),
      ],
      shown,
    );
    expect(moves).toEqual(["ease", null, null, "ease", "ease"]);
  });

  it("drops a show still waiting once the reader moves the map", () => {
    let state = { ...initialCamera(), insets: PANEL };
    state = planCamera(state, input({ focus: focus("trip:b", null, false) }), view()).state;
    state = afterGesture(state, "trip:b");
    const r = planCamera(state, input({ focus: focus("trip:b", OSTRAVA) }), view());
    expect(r.move).toBeNull();
  });

  it("zooms in to a board's stop when the map is far out", () => {
    const stop = { key: "stop:s", ready: true, at: PRAHA, zoomIn: true };
    expect(run([[input({ focus: stop }), view({ zoom: 7 })]])).toEqual(["fly"]);
    expect(run([[input({ focus: stop }), view({ zoom: 13 })]])).toEqual([null]);
  });
});

describe("focusOf", () => {
  const base = { key: "k", tripLoaded: false, tripHasVehicle: false, stopAt: PRAHA };

  it("waits for a trip, then its vehicle", () => {
    expect(focusOf({ ...base, kind: "trip", vehicleAt: undefined }).ready).toBe(false);
    const loading = { ...base, kind: "trip" as const, tripLoaded: true, tripHasVehicle: true };
    expect(focusOf({ ...loading, vehicleAt: undefined }).ready).toBe(false);
    expect(focusOf({ ...loading, vehicleAt: OSTRAVA })).toMatchObject({ ready: true, at: OSTRAVA });
  });

  it("shows the board's stop for a trip without a vehicle", () => {
    const f = focusOf({ ...base, kind: "trip", tripLoaded: true, vehicleAt: undefined });
    expect(f).toMatchObject({ ready: true, at: PRAHA });
  });

  it("is known, with nothing to show, for a vehicle without a position", () => {
    expect(focusOf({ ...base, kind: "vehicle", vehicleAt: null })).toMatchObject({
      ready: true,
      at: null,
    });
  });
});
