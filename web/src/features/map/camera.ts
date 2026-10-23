import { GLIDE_MS } from "./motion";

// Every camera move the app makes, decided in one place. MapLibre cancels a running animation
// whenever another starts, so moves decided apart cut each other off; here each update yields at
// most one move, by these rules in order:
//
// 1. A new navigation (a board, a trip, a vehicle) brings what it is about into view once, as
//    soon as that is known: a flight, only when it is covered by the panels or off screen (a
//    board's stop also when the map is zoomed far out). Loading in between changes nothing;
//    only another navigation starts another show. A gesture of the reader's drops a show still
//    waiting.
// 2. The panels changed (one opened, closed, the sheet snapped): the map keeps what is on screen
//    where it is, unless the followed or selected vehicle ends up covered: then it eases into
//    the open part. While the map moves, this waits for it to stop.
// 3. Following: the camera moves with the vehicle over its glide, except during our flight.

export type LngLat = readonly [number, number];

/** How far the panels cover the map from its left and bottom edges, in CSS pixels. */
export interface Insets {
  left: number;
  bottom: number;
}

export interface Padding {
  top: number;
  right: number;
  bottom: number;
  left: number;
}

/** What the open navigation is about. */
export interface Focus {
  /** Which navigation; "" for none. */
  key: string;
  /** Whether what to show is known (its trip and vehicle have loaded). */
  ready: boolean;
  /** Where it is; null when there is nothing to show (a vehicle without a position). */
  at: LngLat | null;
  /** Zoom in even when `at` is in view (a board's stop on a map zoomed far out). */
  zoomIn: boolean;
}

export interface CameraInput {
  focus: Focus;
  /** The selected vehicle's position, kept clear of the panels. */
  selected: LngLat | null;
  /** The followed vehicle's position; null when nothing is followed. */
  follow: LngLat | null;
  insets: Insets;
}

export interface CameraState {
  /** The navigation already shown (or given up on). */
  shownKey: string;
  /** The insets the camera has taken. */
  insets: Insets;
  /** Where the followed vehicle was last followed to. */
  followAt: LngLat | null;
}

export interface CameraView {
  /** The map is moving: our flight or ease, or the reader. */
  moving: boolean;
  /** Our flight to a focus is under way. */
  flying: boolean;
  zoom: number;
  /** Whether a point is outside the part of the map the insets leave open. */
  covered(at: LngLat, insets: Insets): boolean;
}

export type CameraMove =
  | { kind: "fly"; center: LngLat; zoom: number; padding: Padding }
  | { kind: "ease"; center: LngLat; padding: Padding; duration: number; linear: boolean }
  /** Take new padding, keeping what is on screen where it is. */
  | { kind: "pad"; padding: Padding };

/** A focus is shown at least this close. */
export const SHOW_ZOOM = 14;
/** A board's stop is zoomed to when the map is further out than this. */
const STOP_ZOOM_BELOW = 12;
/** A vehicle the panels came to cover eases out from under them over this long. */
const UNCOVER_MS = 450;

export const NO_INSETS: Insets = { left: 0, bottom: 0 };

export function initialCamera(): CameraState {
  return { shownKey: "", insets: NO_INSETS, followAt: null };
}

export function paddingOf(insets: Insets): Padding {
  return { top: 0, right: 0, bottom: insets.bottom, left: insets.left };
}

const sameInsets = (a: Insets, b: Insets) => a.left === b.left && a.bottom === b.bottom;
const samePoint = (a: LngLat | null, b: LngLat | null) =>
  a === b || (a !== null && b !== null && a[0] === b[0] && a[1] === b[1]);

export function planCamera(
  state: CameraState,
  input: CameraInput,
  view: CameraView,
): { state: CameraState; move: CameraMove | null } {
  const { focus, insets } = input;
  const padding = paddingOf(insets);

  // 1. A new navigation, once what it is about is known.
  if (focus.key !== state.shownKey && focus.ready) {
    const shown = { ...state, shownKey: focus.key };
    const at = focus.at;
    const far = focus.zoomIn && view.zoom < STOP_ZOOM_BELOW;
    // A flight to the navigation before would carry on away from this one.
    if (at && (far || view.flying || view.covered(at, insets))) {
      return {
        state: { ...shown, insets, followAt: input.follow },
        move: { kind: "fly", center: at, zoom: Math.max(view.zoom, SHOW_ZOOM), padding },
      };
    }
    state = shown;
  }

  // 2. The panels changed.
  if (!sameInsets(insets, state.insets)) {
    if (view.moving) return { state, move: null };
    const next = { ...state, insets };
    const keep = input.follow ?? input.selected;
    if (keep && (input.follow !== null || view.covered(keep, insets))) {
      return {
        state: { ...next, followAt: input.follow },
        move: { kind: "ease", center: keep, padding, duration: UNCOVER_MS, linear: false },
      };
    }
    return { state: next, move: { kind: "pad", padding } };
  }

  // 3. Following.
  if (input.follow === null) {
    return { state: state.followAt === null ? state : { ...state, followAt: null }, move: null };
  }
  if (view.flying || samePoint(input.follow, state.followAt)) return { state, move: null };
  return {
    state: { ...state, followAt: input.follow },
    move: { kind: "ease", center: input.follow, padding, duration: GLIDE_MS, linear: true },
  };
}

/** The reader moved the map: a show still waiting for its trip or vehicle is dropped. */
export function afterGesture(state: CameraState, focusKey: string): CameraState {
  return state.shownKey === focusKey ? state : { ...state, shownKey: focusKey };
}

/** What a navigation is about, from what has loaded:
 * - a vehicle: its position, once its answer (or the row it was clicked on) is in;
 * - a trip: its vehicle's position once the trip and the vehicle are in, else, for a trip
 *   without a vehicle, the board's stop;
 * - a board: its stop, zoomed in to when the map is far out. */
export function focusOf(input: {
  key: string;
  kind: "none" | "stop" | "trip" | "vehicle";
  /** The trip has loaded (trips only). */
  tripLoaded: boolean;
  /** Whether the trip has a vehicle (trips only). */
  tripHasVehicle: boolean;
  /** The vehicle's position: undefined while unknown, null when it has none now. */
  vehicleAt: LngLat | null | undefined;
  stopAt: LngLat | null | undefined;
}): Focus {
  const { key, kind, vehicleAt, stopAt } = input;
  switch (kind) {
    case "none":
      return { key, ready: true, at: null, zoomIn: false };
    case "stop":
      return { key, ready: stopAt !== undefined, at: stopAt ?? null, zoomIn: true };
    case "vehicle":
      return { key, ready: vehicleAt !== undefined, at: vehicleAt ?? null, zoomIn: false };
    case "trip":
      if (!input.tripLoaded) return { key, ready: false, at: null, zoomIn: false };
      if (input.tripHasVehicle) {
        return { key, ready: vehicleAt !== undefined, at: vehicleAt ?? null, zoomIn: false };
      }
      return { key, ready: true, at: stopAt ?? null, zoomIn: false };
  }
}
