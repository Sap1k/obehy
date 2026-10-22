// Which calls a collapsed timetable shows (Figma 08, "Trip timetable"): the anchor (the call
// boarded at, else the next one), the two after it, an expander, and the last call. Six calls or
// fewer always show in full. Each hidden range opens on its own: the earlier calls, or the ones
// between the head and the last call.

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

/** Which hidden ranges are open. */
export interface Opened {
  before: boolean;
  middle: boolean;
}

export const CLOSED: Opened = { before: false, middle: false };

/** The collapsed plan with the `open` ranges shown; null when every call shows. */
export function collapse(count: number, anchor: number, open: Opened = CLOSED): Collapsed | null {
  const plan = closedPlan(count, anchor);
  if (!plan) return null;
  const range = ([from, to]: [number, number]) =>
    Array.from({ length: to - from + 1 }, (_, i) => from + i);
  let { before, head, middle } = plan;
  if (open.before && before) {
    head = [...range(before), ...head];
    before = null;
  }
  if (open.middle && middle) {
    head = [...head, ...range(middle)];
    middle = null;
  }
  if (!before && !middle) return null;
  return { before, head, middle, last: plan.last };
}

function closedPlan(count: number, anchor: number): Collapsed | null {
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
