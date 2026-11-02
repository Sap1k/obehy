import type { Schemas } from "../api/client";
import { delayText, toneOf, type Tone } from "./punctuality";

// How late a journey was over the recorded days (`/trips/{id}/history`), between two of its
// stops the rider picks: where they board (its departure) and where they get off (its arrival).
// Outside that segment an early or late run does not matter to them.

export type TripHistory = Schemas["TripHistory"];
export type HistoryDay = Schemas["HistoryDay"];
export type HistoryCall = Schemas["HistoryCall"];

const MINUTE = 60;

/** A delay in seconds, and whether it was inferred rather than seen (history records a stop
 * passed out of sight when the vehicle was about as late either side of it). */
export interface Delay {
  delayS: number | null;
  estimated: boolean;
}

/** A day's run over the segment. */
export interface SegmentDay {
  day: HistoryDay;
  /** The departure delay where the segment starts. */
  from: Delay;
  /** The arrival delay where it ends (its departure when no arrival was seen). */
  to: Delay;
  /** The largest delay at any of its stops. */
  worst: Delay;
  /** The day's calls within the segment, in order. */
  calls: HistoryCall[];
}

const NONE: Delay = { delayS: null, estimated: false };

/** A call's delay as the rider meets it: its departure, at the segment's end its arrival. */
export function callDelay(call: HistoryCall, end: boolean): Delay {
  const arrival = { delayS: call.arrival_delay_s, estimated: call.arrival_estimated };
  const departure = { delayS: call.departure_delay_s, estimated: call.departure_estimated };
  const [first, second] = end ? [arrival, departure] : [departure, arrival];
  return first.delayS !== null ? first : second.delayS !== null ? second : NONE;
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
    if (first < 0 || last < 0) return { day, from: NONE, to: NONE, worst: NONE, calls: [] };
    const calls = day.calls.slice(first, last + 1);
    const delays = calls.map((c, i) => callDelay(c, i === calls.length - 1));
    const worst = delays.reduce(
      (most, d) =>
        d.delayS !== null && (most.delayS === null || d.delayS > most.delayS) ? d : most,
      NONE,
    );
    return {
      day,
      from: delays[0]!,
      to: delays.at(-1)!,
      worst,
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
    .filter((d) => !d.day.cancelled && d.to.delayS !== null)
    .map((d) => d.to.delayS!)
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
