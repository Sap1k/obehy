import type { Schemas } from "../api/client";
import { delayText, toneOf, type Tone } from "./punctuality";

// How late a journey was over the recorded days (`/trips/{id}/history`), between two of its
// stops the rider picks: where they board (its departure) and where they get off (its arrival).
// Outside that segment an early or late run does not matter to them.

export type TripHistory = Schemas["TripHistory"];
export type HistoryDay = Schemas["HistoryDay"];
export type HistoryCall = Schemas["HistoryCall"];

const MINUTE = 60;

/** A day's run over the segment. */
export interface SegmentDay {
  day: HistoryDay;
  /** The departure delay where the segment starts, in seconds. */
  from: number | null;
  /** The arrival delay where it ends (its departure when no arrival was seen). */
  to: number | null;
  /** The largest delay at any of its stops. */
  worst: number | null;
  /** The day's calls within the segment, in order. */
  calls: HistoryCall[];
}

/** A call's delay as the rider meets it: its departure, at the segment's end its arrival. */
function delayAt(call: HistoryCall, end: boolean): number | null {
  return end
    ? (call.arrival_delay_s ?? call.departure_delay_s)
    : (call.departure_delay_s ?? call.arrival_delay_s);
}

/** Each day's run from the `from`th to the `to`th of the journey's stops (`history.stops`), a
 * stop found in a day's calls by its location, its first visit after the start; latest day
 * first, as the days come. */
export function segmentDays(history: TripHistory, from: number, to: number): SegmentDay[] {
  const start = history.stops[from]?.location_id;
  const end = history.stops[to]?.location_id;
  return history.days.map((day) => {
    const first = day.calls.findIndex((c) => c.location_id === start);
    const last = first < 0 ? -1 : day.calls.findIndex((c, i) => i > first && c.location_id === end);
    if (first < 0 || last < 0) return { day, from: null, to: null, worst: null, calls: [] };
    const calls = day.calls.slice(first, last + 1);
    const delays = calls.flatMap((c, i) => {
      const d = delayAt(c, i === calls.length - 1);
      return d === null ? [] : [d];
    });
    return {
      day,
      from: delayAt(calls[0]!, false),
      to: delayAt(calls.at(-1)!, true),
      worst: delays.length ? Math.max(...delays) : null,
      calls,
    };
  });
}

/** How the segment usually ends: the share of measured runs on time at its end and their
 * median delay there; null without a measured run. */
export function segmentSummary(
  days: SegmentDay[],
): { runs: number; onTime: number; median: number } | null {
  const ends = days
    .filter((d) => !d.day.cancelled && d.to !== null)
    .map((d) => d.to!)
    .sort((a, b) => a - b);
  if (ends.length === 0) return null;
  const onTime = ends.filter((d) => toneOf(Math.round(d / MINUTE)) === "onTime").length;
  const middle = ends.length >> 1;
  const median = ends.length % 2 ? ends[middle]! : (ends[middle - 1]! + ends[middle]!) / 2;
  return { runs: ends.length, onTime: onTime / ends.length, median };
}

/** The tone of a delay in seconds, by the boards' bands. */
export function delayTone(delayS: number | null): Tone {
  return delayS === null ? "unknown" : toneOf(Math.round(delayS / MINUTE));
}

/** "včas", "+3 min", "−1 min", in whole minutes, to follow "obvykle" or stand alone. */
export function delayWords(delayS: number): string {
  const words = delayText(Math.round(delayS / MINUTE));
  return words === "Včas" ? "včas" : words;
}
