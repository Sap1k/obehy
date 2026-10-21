// Every time is shown in Prague, whatever the browser's zone, and cut to the minute: a delay is
// the difference of the minutes shown, so 14:30 → 14:34 always reads "+4 min".

export const ZONE = "Europe/Prague";

const MINUTE = 60_000;

const clock = new Intl.DateTimeFormat("cs-CZ", {
  timeZone: ZONE,
  hour: "2-digit",
  minute: "2-digit",
  hourCycle: "h23",
});

const dayKeyFormat = new Intl.DateTimeFormat("en-CA", {
  timeZone: ZONE,
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
});

const longDay = new Intl.DateTimeFormat("cs-CZ", {
  timeZone: ZONE,
  weekday: "long",
  day: "numeric",
  month: "long",
});

/** The instant cut down to its minute, in epoch milliseconds. */
export function minuteOf(iso: string | Date): number {
  const ms = typeof iso === "string" ? Date.parse(iso) : iso.getTime();
  return Math.floor(ms / MINUTE) * MINUTE;
}

/** "14:36" in Prague. */
export function formatClock(iso: string | Date): string {
  return clock.format(minuteOf(iso));
}

/** The Prague calendar date, "2026-10-21". */
export function dayKey(iso: string | Date | number): string {
  return dayKeyFormat.format(typeof iso === "string" ? Date.parse(iso) : iso);
}

/** Whole minutes between the two minutes shown. */
export function minutesBetween(from: string, to: string): number {
  return Math.round((minuteOf(to) - minuteOf(from)) / MINUTE);
}

function addDays(key: string, days: number): string {
  const [y, m, d] = key.split("-").map(Number) as [number, number, number];
  const date = new Date(Date.UTC(y, m - 1, d + days, 12));
  return date.toISOString().slice(0, 10);
}

function capitalise(text: string): string {
  return text.charAt(0).toLocaleUpperCase("cs-CZ") + text.slice(1);
}

/** "Středa 21. října". */
export function formatLongDay(iso: string | Date | number): string {
  return capitalise(longDay.format(typeof iso === "string" ? Date.parse(iso) : iso));
}

/** "Dnes", "Zítra", "Včera" or "Čtvrtek 23. října", relative to `now`. */
export function relativeDay(iso: string | Date | number, now: Date | number): string {
  const key = dayKey(iso);
  const today = dayKey(now);
  if (key === today) return "Dnes";
  if (key === addDays(today, 1)) return "Zítra";
  if (key === addDays(today, -1)) return "Včera";
  return formatLongDay(iso);
}

/** The divider between two days of a board: "Zítra · čtvrtek 22. října". */
export function dayDividerLabel(iso: string, now: Date | number): string {
  const relative = relativeDay(iso, now);
  const long = longDay.format(Date.parse(iso));
  return relative === formatLongDay(iso) ? relative : `${relative} · ${long}`;
}

/** The offset of Prague from UTC at `ms`, in minutes (60 or 120). */
function pragueOffset(ms: number): number {
  const parts = new Intl.DateTimeFormat("en-US", {
    timeZone: ZONE,
    timeZoneName: "longOffset",
  }).formatToParts(ms);
  const name = parts.find((p) => p.type === "timeZoneName")?.value ?? "GMT+01:00";
  const match = /GMT([+-])(\d{2}):(\d{2})/.exec(name);
  if (!match) return 60;
  const sign = match[1] === "-" ? -1 : 1;
  return sign * (Number(match[2]) * 60 + Number(match[3]));
}

/** The instant of a Prague wall-clock time: `day` "2026-10-21", `time` "14:30". Across a DST
 * change the offset is the one in force at that wall time. */
export function pragueInstant(day: string, time: string): Date {
  const [y, m, d] = day.split("-").map(Number) as [number, number, number];
  const [hh, mm] = time.split(":").map(Number) as [number, number];
  const wall = Date.UTC(y, m - 1, d, hh, mm);
  let guess = wall - pragueOffset(wall) * MINUTE;
  guess = wall - pragueOffset(guess) * MINUTE;
  return new Date(guess);
}

/** The ISO 8601 form with an offset the API asks for. */
export function toApiInstant(date: Date): string {
  return date.toISOString();
}

export { addDays };
