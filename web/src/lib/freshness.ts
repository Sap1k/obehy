import { useSyncExternalStore } from "react";

// How fresh the screen's data is (Figma "Data status"): live, delayed, or offline with the last
// answer kept. The screen that polls reports it; the shell shows it.

export type Freshness = "live" | "delayed" | "offline";

/** An answer older than this reads as delayed. */
export const DELAYED_AFTER_MS = 60_000;

export function freshnessOf(input: {
  failing: boolean;
  online: boolean;
  generatedAt: string | null | undefined;
  anyStale: boolean;
  now: number;
}): Freshness {
  if (input.failing || !input.online) return "offline";
  if (input.anyStale) return "delayed";
  if (input.generatedAt && input.now - Date.parse(input.generatedAt) > DELAYED_AFTER_MS) {
    return "delayed";
  }
  return "live";
}

let current: Freshness | null = null;
const listeners = new Set<() => void>();

export function reportFreshness(value: Freshness | null): void {
  if (value === current) return;
  current = value;
  for (const listener of listeners) listener();
}

export function useReportedFreshness(): Freshness | null {
  return useSyncExternalStore(
    (listener) => {
      listeners.add(listener);
      return () => listeners.delete(listener);
    },
    () => current,
  );
}
