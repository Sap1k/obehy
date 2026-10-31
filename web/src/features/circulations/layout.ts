import type { ChainJourney, ScopedChain, ScopeRoute } from "../../api/client";
import { pragueInstant } from "../../lib/time";

// The oběhy page's day timeline laid out: one row per vehicle, its chains (its journeys in a
// row, split where it stood for hours) one after another, each journey a bar from its scheduled departure to its arrival (where the vehicle was seen
// when history has no schedule), in seconds from the service date's midnight in Prague, so a
// chain past midnight runs on past 24:00. Pure.

export interface TimelineBar {
  journey: ChainJourney;
  start: number;
  end: number;
  /** Of the scope asked for (the companies and modes, or the route); others are dimmed. */
  inScope: boolean;
  /** The trip the page was opened from. */
  highlighted: boolean;
  /** Which of the row's chains it is of. */
  part: number;
}

export interface TimelineRow {
  key: string;
  /** The vehicle's first chain of the day, for what the row says of the vehicle. */
  chain: ScopedChain;
  chains: ScopedChain[];
  bars: TimelineBar[];
  /** Where the vehicle stood between its chains, in seconds as the bars. */
  breaks: Span[];
}

export interface Span {
  start: number;
  end: number;
}

const HOUR = 3600;
const BY_ID = new Intl.Collator("cs-CZ", { numeric: true });

/** A row's vehicle as its operator numbers it (the registry's fleet number or label), else as
 * its source does. */
export function vehicleName(chain: ScopedChain): string {
  return chain.fleet_number ?? chain.public_label ?? chain.source_vehicle_id;
}

/** "Iveco Crossway LE LINE 13M", when the registry knows it. */
export function vehicleModel(chain: ScopedChain): string | null {
  return [chain.manufacturer, chain.model].filter(Boolean).join(" ") || null;
}

function secondsFrom(origin: number, iso: string): number {
  return Math.round((Date.parse(iso) - origin) / 1000);
}

/** A row per vehicle of the chains whose vehicle matches `vehicle` (its IDs or fleet number,
 * any part, any case), by the vehicle's name (numbers as numbers), its chains in the order they
 * start. */
export function rowsOf(
  chains: ScopedChain[],
  serviceDate: string,
  { vehicle, tripId }: { vehicle?: string; tripId?: string },
): TimelineRow[] {
  const origin = pragueInstant(serviceDate, "00:00").getTime();
  const wanted = vehicle?.trim().toLocaleLowerCase("cs-CZ");
  const byVehicle = new Map<string, ScopedChain[]>();
  for (const c of chains) {
    const matches =
      !wanted ||
      [c.source_vehicle_id, c.vehicle_id, c.fleet_number, c.public_label].some((id) =>
        id?.toLocaleLowerCase("cs-CZ").includes(wanted),
      );
    if (matches) byVehicle.set(c.vehicle_id, [...(byVehicle.get(c.vehicle_id) ?? []), c]);
  }
  const rows = [...byVehicle].map(([vehicleId, theirs]): TimelineRow => {
    const ordered = [...theirs].sort((a, b) => Date.parse(a.first_seen) - Date.parse(b.first_seen));
    const bars = ordered.flatMap((chain, part) => {
      const matched = new Set(chain.matched);
      return chain.journeys.map((journey, i) => {
        const start = secondsFrom(origin, journey.departure ?? journey.first_seen);
        const end = secondsFrom(origin, journey.arrival ?? journey.last_seen);
        return {
          journey,
          start,
          end: Math.max(end, start + 60),
          inScope: matched.has(i),
          highlighted: tripId !== undefined && journey.trip_id === tripId,
          part,
        };
      });
    });
    const breaks: Span[] = [];
    for (let i = 1; i < bars.length; i++) {
      const [before, after] = [bars[i - 1]!, bars[i]!];
      if (after.part !== before.part && after.start > before.end) {
        breaks.push({ start: before.end, end: after.start });
      }
    }
    return {
      key: `${vehicleId}@${serviceDate}`,
      chain: ordered[0]!,
      chains: ordered,
      bars,
      breaks,
    };
  });
  return rows.sort((a, b) => BY_ID.compare(vehicleName(a.chain), vehicleName(b.chain)));
}

/** Whole hours from the earliest bar's start to the latest bar's end. */
export function spanOf(rows: TimelineRow[]): Span | null {
  const bars = rows.flatMap((r) => r.bars);
  if (bars.length === 0) return null;
  const start = Math.min(...bars.map((b) => b.start));
  const end = Math.max(...bars.map((b) => b.end));
  return {
    start: Math.floor(start / HOUR) * HOUR,
    end: Math.max(Math.ceil(end / HOUR) * HOUR, Math.floor(start / HOUR) * HOUR + HOUR),
  };
}

/** The span's whole hours, as hours from the service date's midnight (25 is 1:00 next day);
 * every `step`th of the day's hours when an hour is too narrow for its label. */
export function hourTicks(span: Span, step = 1): number[] {
  const ticks: number[] = [];
  for (let h = span.start / HOUR; h <= span.end / HOUR; h++) if (h % step === 0) ticks.push(h);
  return ticks;
}

/** Every how many hours a label fits at `hourPx` an hour. */
export function tickStep(hourPx: number): number {
  return hourPx >= 32 ? 1 : hourPx >= 16 ? 2 : hourPx >= 8 ? 3 : 6;
}

/** The narrowest and widest hour the timeline zooms to; the narrowest is the whole day's fit
 * when that is wider. */
export const ZOOM_PX = { min: 6, max: 640 };

/** The hour width `factor` times `hourPx`, within the zoom's bounds (`fitPx` the least). */
export function zoomed(hourPx: number, factor: number, fitPx: number): number {
  const least = Math.min(Math.max(ZOOM_PX.min, fitPx), ZOOM_PX.max);
  return Math.min(ZOOM_PX.max, Math.max(least, hourPx * factor));
}

/** Where to scroll so the time under `pointerX` (px from the scroller's left edge, past a
 * `labelPx` column) stays under it when an hour goes from `fromPx` to `toPx` wide. */
export function zoomAround(
  scrollLeft: number,
  pointerX: number,
  labelPx: number,
  fromPx: number,
  toPx: number,
): number {
  const lane = Math.max(0, pointerX - labelPx);
  const hours = (scrollLeft + lane) / fromPx;
  return Math.max(0, hours * toPx - lane);
}

/** "5", "23", "0" (the next day's midnight), "1". */
export function hourLabel(hour: number): string {
  return String(((hour % 24) + 24) % 24);
}

/** Where `seconds` falls in the span, in % from the left. */
export function percent(seconds: number, span: Span): number {
  return ((seconds - span.start) / (span.end - span.start)) * 100;
}

/** Seconds from the service date's midnight to `now`. */
export function secondsOfDay(serviceDate: string, now: number): number {
  return Math.round((now - pragueInstant(serviceDate, "00:00").getTime()) / 1000);
}

/** A journey's ends cut for a bar, as a headsign would put them (JDF names run town, part,
 * stop): within one town without it ("Na Knížecí → Olšanské hřbitovy"), between towns only
 * the towns ("Chlumec → Ústí n.L."). */
export function shortRoute(origin: string, destination: string): string {
  const parts = (name: string) =>
    name
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean);
  const [from, to] = [parts(origin), parts(destination)];
  const town = (p: string[], name: string) => p[0] ?? name;
  const within = (p: string[], name: string) =>
    p.length > 1 ? p.slice(1).join(", ") : town(p, name);
  return town(from, origin) === town(to, destination)
    ? `${within(from, origin)} → ${within(to, destination)}`
    : `${town(from, origin)} → ${town(to, destination)}`;
}

/** The rows to draw of `count`, each `rowPx` tall under a `headPx` head, when the scroller is
 * scrolled `top` and shows `height`: those in view and `spare` either side. */
export function windowOf(
  top: number,
  height: number,
  count: number,
  { rowPx, headPx, spare }: { rowPx: number; headPx: number; spare: number },
): { first: number; last: number } {
  const from = Math.floor((top - headPx) / rowPx) - spare;
  const to = Math.ceil((top - headPx + height) / rowPx) + spare;
  const first = Math.min(Math.max(0, from), count);
  return { first, last: Math.min(Math.max(first, to), count) };
}

/** Lower case without diacritics, as the API's search compares. */
function folded(text: string): string {
  return text.normalize("NFD").replace(/\p{M}/gu, "").toLocaleLowerCase("cs-CZ");
}

/** The lines whose number or name holds the words of `q` in order, regardless of case and
 * diacritics: the number itself first, then numbers starting so, then the rest, each in the
 * lines' order. */
export function linesMatching(routes: ScopeRoute[], q: string): ScopeRoute[] {
  const words = folded(q)
    .split(/[^\p{L}\p{N}]+/u)
    .filter(Boolean);
  if (words.length === 0) return [];
  const holds = (text: string) => {
    let at = 0;
    for (const w of words) {
      const found = text.indexOf(w, at);
      if (found < 0) return false;
      at = found + w.length;
    }
    return true;
  };
  const rank = (r: ScopeRoute) => {
    const number = folded(r.line.short_name ?? "");
    return number === words.join(" ") ? 0 : number.startsWith(words[0]!) ? 1 : 2;
  };
  return routes
    .filter((r) => holds(folded(`${r.line.short_name ?? ""} ${r.line.long_name ?? ""}`)))
    .map((r, i) => ({ r, i, rank: rank(r) }))
    .sort((a, b) => a.rank - b.rank || a.i - b.i)
    .map(({ r }) => r);
}
