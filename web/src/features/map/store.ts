import { useEffect, useId, useSyncExternalStore } from "react";
import type { Vehicle } from "../../api/client";

// What the map and the panels over it tell each other. On the desktop the map lives in the
// shell, under every tab, beside whatever panels the route shows; on the phone it lives in the
// map route. Panels say what the map should show (a board its stop, a trip panel its trip, the
// vehicle panel its vehicle); the map says which vehicle is open and whether it is followed.

export interface Focus {
  /** A board's stop place: marked, and brought into view once. */
  stop?: { id: string; latitude: number; longitude: number };
  /** A trip: its path and stops, and its vehicle selected. */
  trip?: { tripId: string; date: string };
  /** The vehicle panel's vehicle. */
  vehicleId?: string;
}

export interface MapStoreState {
  /** Every mounted panel's focus, merged in mount order. */
  focus: Focus;
  /** The open vehicle: its own answer, else the row it was clicked on; null without one. */
  vehicle: Vehicle | null;
  /** The vehicle followed, by ID. */
  followedId: string | null;
  /** How far from the left the panels cover the map, in CSS pixels. */
  insetLeft: number;
}

const owners = new Map<string, Focus>();
let state: MapStoreState = { focus: {}, vehicle: null, followedId: null, insetLeft: 0 };
const listeners = new Set<() => void>();

function update(next: Partial<MapStoreState>): void {
  state = { ...state, ...next };
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

function mergedFocus(): Focus {
  return Object.assign({}, ...owners.values()) as Focus;
}

export function useMapStore(): MapStoreState {
  return useSyncExternalStore(subscribe, () => state);
}

/** A panel's focus while it is mounted; `undefined` fields are left to other panels. */
export function useMapFocus(focus: Focus): void {
  const owner = useId();
  const key = JSON.stringify(focus);
  useEffect(() => {
    const parsed = JSON.parse(key) as Focus;
    owners.set(owner, parsed);
    update({ focus: mergedFocus() });
    return () => {
      owners.delete(owner);
      update({ focus: mergedFocus() });
    };
  }, [owner, key]);
}

export function setOpenVehicle(vehicle: Vehicle | null): void {
  if (vehicle !== state.vehicle) update({ vehicle });
}

/** Follow the open vehicle, or stop. */
export function setFollowing(on: boolean): void {
  const id = on ? (state.focus.vehicleId ?? state.vehicle?.vehicle_id ?? null) : null;
  if (id !== state.followedId) update({ followedId: id });
}

export function setInsetLeft(px: number): void {
  if (px !== state.insetLeft) update({ insetLeft: px });
}
