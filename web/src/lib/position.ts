import { useEffect, useSyncExternalStore } from "react";

// Where the user is, when they let the site know: the map's "Moje poloha" reports each fix, and
// a search asks the browser for a fix only when the site already has the permission (never a
// prompt of its own). The position stays in the browser; what leaves it is rounded (`roughly`).

export interface Position {
  latitude: number;
  longitude: number;
  /** Epoch milliseconds of the fix. */
  at: number;
}

/** A fix older than this is asked for again. */
const FRESH_MS = 2 * 60_000;

let current: Position | null = null;
const listeners = new Set<() => void>();

export function rememberPosition(latitude: number, longitude: number): void {
  current = { latitude, longitude, at: Date.now() };
  for (const listener of listeners) listener();
}

function subscribe(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The last known position; asks the browser for a fresh one while mounted, if (and only if)
 * the site may already know it. */
export function useKnownPosition(): Position | null {
  const position = useSyncExternalStore(subscribe, () => current);
  useEffect(() => {
    if (current && Date.now() - current.at < FRESH_MS) return;
    let live = true;
    void navigator.permissions
      ?.query({ name: "geolocation" })
      .then((state) => {
        if (!live || state.state !== "granted") return;
        navigator.geolocation.getCurrentPosition(
          (p) => rememberPosition(p.coords.latitude, p.coords.longitude),
          () => undefined,
          { maximumAge: FRESH_MS, timeout: 10_000 },
        );
      })
      .catch(() => undefined);
    return () => {
      live = false;
    };
  }, []);
  return position;
}

/** A position rounded to about a kilometre: enough to order results, not to find someone. */
export function roughly(p: Position): string {
  return `${p.latitude.toFixed(2)},${p.longitude.toFixed(2)}`;
}

/** Metres between two points (haversine). */
export function metresBetween(a: Position, lat: number, lon: number): number {
  const rad = Math.PI / 180;
  const dLat = (lat - a.latitude) * rad;
  const dLon = (lon - a.longitude) * rad;
  const h =
    Math.sin(dLat / 2) ** 2 +
    Math.cos(a.latitude * rad) * Math.cos(lat * rad) * Math.sin(dLon / 2) ** 2;
  return 2 * 6_371_000 * Math.asin(Math.sqrt(h));
}

/** "350 m", "2,4 km", "18 km". */
export function distanceText(metres: number): string {
  if (metres < 1000) return `${Math.max(10, Math.round(metres / 10) * 10)} m`;
  const km = metres / 1000;
  return km < 10 ? `${km.toFixed(1).replace(".", ",")} km` : `${Math.round(km)} km`;
}
