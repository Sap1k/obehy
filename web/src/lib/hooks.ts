import { useEffect, useState } from "react";

/** The current time, ticking every `everyMs`. */
export function useNow(everyMs = 1000): number {
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const id = window.setInterval(() => setNow(Date.now()), everyMs);
    return () => window.clearInterval(id);
  }, [everyMs]);
  return now;
}

/** "před 12 s", "před 3 min". */
export function ago(iso: string, now: number): string {
  const s = Math.max(0, Math.round((now - Date.parse(iso)) / 1000));
  if (s < 60) return `před ${s} s`;
  const m = Math.floor(s / 60);
  if (m < 60) return `před ${m} min`;
  return `před ${Math.floor(m / 60)} h`;
}
