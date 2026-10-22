import { useSyncExternalStore } from "react";

// How fresh the realtime is (Figma "Data status"): live, delayed, or offline with the last
// answer kept. The shell asks /status how recently any source's vehicle report came in (the
// worker writes them every tick): that is whether the data is live. One quiet trip on a board
// is not; its own row says so. Pages only report that their own requests fail.

export type Freshness = "live" | "delayed" | "offline";

/** The newest vehicle report older than this reads as delayed: the worker or every source is
 * behind. Sources report every 10–30 s. */
export const DELAYED_AFTER_MS = 120_000;

export function freshnessOf(input: {
  failing: boolean;
  online: boolean;
  /** The newest vehicle report of any source (`/status` `realtime_at`). */
  realtimeAt: string | null | undefined;
  now: number;
}): Freshness {
  if (input.failing || !input.online) return "offline";
  if (!input.realtimeAt || input.now - Date.parse(input.realtimeAt) > DELAYED_AFTER_MS) {
    return "delayed";
  }
  return "live";
}

// Pages whose requests are failing, by page.
const failing = new Set<string>();
let anyFailing = false;
const listeners = new Set<() => void>();

/** A page's requests fail (true) or work again (false, also when it goes away). */
export function reportFailing(page: string, isFailing: boolean): void {
  if (isFailing) failing.add(page);
  else failing.delete(page);
  const next = failing.size > 0;
  if (next === anyFailing) return;
  anyFailing = next;
  for (const listener of listeners) listener();
}

export function useReportedFailing(): boolean {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => anyFailing,
  );
}
