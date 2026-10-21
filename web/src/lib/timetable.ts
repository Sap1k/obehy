// Which calls a collapsed timetable shows (Figma 08, "Trip timetable"): the anchor (the call
// boarded at, else the next one), the two after it, an expander, and the last call. Six calls or
// fewer always show in full.

export const SHOW_ALL_UP_TO = 6;
export const AFTER_ANCHOR = 2;

export interface Collapsed {
  /** Indexes before the anchor, hidden behind "earlier stops". */
  before: [number, number] | null;
  /** Indexes shown at the top: the anchor and those after it. */
  head: number[];
  /** Indexes hidden behind the expander. */
  middle: [number, number] | null;
  /** The last call, when not already in `head`. */
  last: number | null;
}

export function collapse(count: number, anchor: number): Collapsed | null {
  if (count <= SHOW_ALL_UP_TO) return null;
  const a = Math.min(Math.max(anchor, 0), count - 1);
  const headEnd = Math.min(a + AFTER_ANCHOR, count - 1);
  const head: number[] = [];
  for (let i = a; i <= headEnd; i++) head.push(i);
  const lastIndex = count - 1;
  const last = headEnd < lastIndex ? lastIndex : null;
  // A gap of a single call is not worth an expander: show it.
  let middle: [number, number] | null = null;
  if (last !== null && last - headEnd > 1) {
    if (last - headEnd === 2) head.push(headEnd + 1);
    else middle = [headEnd + 1, last - 1];
  }
  return { before: a > 0 ? [0, a - 1] : null, head, middle, last };
}

/** The anchor: the first call at `locationId` when given, else the first call not yet passed. */
export function anchorIndex(
  calls: readonly { location_id: string }[],
  passed: readonly boolean[],
  locationId?: string,
): number {
  if (locationId) {
    const found = calls.findIndex((c) => c.location_id === locationId);
    if (found >= 0) return found;
  }
  const next = passed.findIndex((p) => !p);
  return next >= 0 ? next : Math.max(calls.length - 1, 0);
}
