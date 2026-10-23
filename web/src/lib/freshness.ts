import { useQueryClient } from "@tanstack/react-query";
import { useCallback, useSyncExternalStore } from "react";

// How fresh the realtime is (Figma "Data status"): live, delayed, or offline with the last
// answer kept. The shell asks /status how recently any source's vehicle report came in (the
// worker writes them every tick): that is whether the data is live. One quiet trip on a board
// is not; its own row says so. Failing requests are read from the query cache.

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

/** Whether a request the data status counts (queries with `meta.status`: boards, trips, the
 * map's cells) is failing now; read from the query cache, so pages report nothing. */
export function useStatusQueriesFailing(): boolean {
  const cache = useQueryClient().getQueryCache();
  const subscribe = useCallback((listener: () => void) => cache.subscribe(listener), [cache]);
  return useSyncExternalStore(subscribe, () =>
    cache
      .findAll({ type: "active" })
      .some((q) => q.meta?.status === true && q.state.status === "error"),
  );
}
