import { useSyncExternalStore } from "react";
import type { SheetSnap } from "../../lib/sheet";

// The map's state that is neither in the URL (what is open) nor an API answer (the query
// cache): which vehicle is followed, how far the desktop's panels reach over the map, and
// the phone's sheet snap. Written only from event handlers and observers; read with a selector,
// so a component re-renders for its own slice only.

export interface MapStoreState {
  /** The vehicle followed, by ID. */
  followedId: string | null;
  /** How far from the left the desktop's panels cover the map, in CSS pixels. */
  panelsRight: number;
  /** The phone's sheet. */
  snap: SheetSnap;
}

let state: MapStoreState = { followedId: null, panelsRight: 0, snap: "half" };
const listeners = new Set<() => void>();

function update(next: Partial<MapStoreState>): void {
  const merged = { ...state, ...next };
  if ((Object.keys(next) as (keyof MapStoreState)[]).every((k) => merged[k] === state[k])) return;
  state = merged;
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** A slice of the store; return a primitive or a stored value, not a new object. */
export function useMapStore<T>(select: (s: MapStoreState) => T): T {
  return useSyncExternalStore(subscribe, () => select(state));
}

export function follow(vehicleId: string): void {
  update({ followedId: vehicleId });
}

export function unfollow(): void {
  update({ followedId: null });
}

export function setPanelsRight(px: number): void {
  update({ panelsRight: px });
}

export function setSnap(snap: SheetSnap): void {
  update({ snap });
}

/** For tests: the store as new. */
export function resetMapStore(): void {
  state = { followedId: null, panelsRight: 0, snap: "half" };
}
