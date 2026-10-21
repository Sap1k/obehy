import type { Alert } from "../api/client";
import { ZONE } from "./time";

/** The Czech text of a translated field, else English, else any. */
export function localised(texts: Record<string, string> | undefined): string {
  if (!texts) return "";
  return texts.cs ?? texts.en ?? Object.values(texts)[0] ?? "";
}

const shortDate = new Intl.DateTimeFormat("cs-CZ", {
  timeZone: ZONE,
  day: "numeric",
  month: "numeric",
});

/** "do 31. 10." or "od 22. 10." for the period in force now or next; empty when always. */
export function periodText(alert: Alert, now: number): string {
  const current =
    alert.periods.find(
      (p) => (!p.start || Date.parse(p.start) <= now) && (!p.end || Date.parse(p.end) > now),
    ) ?? alert.periods[0];
  if (!current) return "";
  if (current.start && Date.parse(current.start) > now) {
    return `od ${shortDate.format(Date.parse(current.start))}`;
  }
  return current.end ? `do ${shortDate.format(Date.parse(current.end))}` : "";
}

/** Alerts informing any of the board's stop places (shown as banners above the rows). */
export function stopAlerts(alerts: readonly Alert[], stopIds: readonly string[]): Alert[] {
  const ids = new Set(stopIds);
  return alerts.filter((a) =>
    a.informs.some(
      (t) => t.kind === "stop" && (ids.has(t.id) || (t.stop_place && ids.has(t.stop_place))),
    ),
  );
}
