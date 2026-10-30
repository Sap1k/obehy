import type { ChainJourney, ScopedChain, ScopeRoute } from "../../api/client";
import { pragueInstant } from "../../lib/time";

// The oběhy page's day timeline laid out: one row per chain (a vehicle's journeys in a row),
// each journey a bar from its scheduled departure to its arrival (where the vehicle was seen
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
}

export interface TimelineRow {
  key: string;
  chain: ScopedChain;
  bars: TimelineBar[];
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

/** The rows of the chains whose vehicle matches `vehicle` (its IDs or fleet number, any part,
 * any case), by the vehicle's name (numbers as numbers), a vehicle's chains by when they
 * start. */
export function rowsOf(
  chains: ScopedChain[],
  serviceDate: string,
  { vehicle, tripId }: { vehicle?: string; tripId?: string },
): TimelineRow[] {
  const origin = pragueInstant(serviceDate, "00:00").getTime();
  const wanted = vehicle?.trim().toLocaleLowerCase("cs-CZ");
  const rows = chains
    .filter(
      (c) =>
        !wanted ||
        [c.source_vehicle_id, c.vehicle_id, c.fleet_number, c.public_label].some((id) =>
          id?.toLocaleLowerCase("cs-CZ").includes(wanted),
        ),
    )
    .map((chain) => {
      const matched = new Set(chain.matched);
      const bars = chain.journeys.map((journey, i) => {
        const start = secondsFrom(origin, journey.departure ?? journey.first_seen);
        const end = secondsFrom(origin, journey.arrival ?? journey.last_seen);
        return {
          journey,
          start,
          end: Math.max(end, start + 60),
          inScope: matched.has(i),
          highlighted: tripId !== undefined && journey.trip_id === tripId,
        };
      });
      return { key: `${chain.vehicle_id}@${chain.first_seen}`, chain, bars };
    });
  const first = (r: TimelineRow) => r.bars[0]?.start ?? 0;
  return rows.sort(
    (a, b) => BY_ID.compare(vehicleName(a.chain), vehicleName(b.chain)) || first(a) - first(b),
  );
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

/** The span's whole hours, as hours from the service date's midnight (25 is 1:00 next day). */
export function hourTicks(span: Span): number[] {
  const ticks: number[] = [];
  for (let h = span.start / HOUR; h <= span.end / HOUR; h++) ticks.push(h);
  return ticks;
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
