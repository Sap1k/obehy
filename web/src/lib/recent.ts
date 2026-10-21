// Recently opened stop places, per browser: a convenience only, so every access may fail
// (private windows, blocked storage) and the page works without it.

const KEY = "obehy.recent-stops";
const KEEP = 6;

export interface RecentStop {
  location_id: string;
  name: string;
  municipality: string | null;
}

export function readRecent(): RecentStop[] {
  try {
    const raw = window.localStorage.getItem(KEY);
    const parsed: unknown = raw ? JSON.parse(raw) : [];
    return Array.isArray(parsed) ? (parsed as RecentStop[]).slice(0, KEEP) : [];
  } catch {
    return [];
  }
}

export function rememberStop(stop: RecentStop): void {
  try {
    const rest = readRecent().filter((s) => s.location_id !== stop.location_id);
    const entry = {
      location_id: stop.location_id,
      name: stop.name,
      municipality: stop.municipality,
    };
    window.localStorage.setItem(KEY, JSON.stringify([entry, ...rest].slice(0, KEEP)));
  } catch {
    // Storage unavailable: nothing to remember.
  }
}
