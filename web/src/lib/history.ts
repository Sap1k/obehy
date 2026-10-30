import type { Schemas } from "../api/client";
import { delayText, toneOf, type Tone } from "./punctuality";

// How late a journey was at its stops over the recorded days (`/trips/{id}/history`), drawn as
// range bars on one scale: the median, then to the 90th percentile, and a dot at the worst.

export type HistoryStop = Schemas["HistoryStop"];

/** The delays a range covers, in seconds. */
export interface Scale {
  min: number;
  max: number;
}

const MINUTE = 60;

/** From a minute early (where on time begins) or the earliest median, to five minutes or the
 * largest 90th percentile, in whole minutes. The worst runs do not stretch it: one outlier
 * would flatten every bar. */
export function delayScale(stops: HistoryStop[]): Scale {
  const medians = stops.flatMap((s) => (s.median_delay_s === null ? [] : [s.median_delay_s]));
  const p90s = stops.flatMap((s) => (s.p90_delay_s === null ? [] : [s.p90_delay_s]));
  const min = Math.min(-MINUTE, ...medians);
  const max = Math.max(5 * MINUTE, ...p90s);
  return {
    min: Math.floor(min / MINUTE) * MINUTE,
    max: Math.ceil(max / MINUTE) * MINUTE,
  };
}

/** Where a delay falls on the scale, in % from the left, held to its ends. */
export function place(delayS: number, scale: Scale): number {
  const at = ((delayS - scale.min) / (scale.max - scale.min)) * 100;
  return Math.min(100, Math.max(0, at));
}

export interface RangeBar {
  /** % from the left: on time (zero), the median, the 90th percentile, the worst. */
  zero: number;
  median: number;
  p90: number | null;
  worst: number | null;
  /** The worst run is later than the scale reaches. */
  beyond: boolean;
  tone: Tone;
}

/** A stop's bar, or null when no run has a time there. */
export function rangeBar(stop: HistoryStop, scale: Scale): RangeBar | null {
  if (stop.median_delay_s === null) return null;
  return {
    zero: place(0, scale),
    median: place(stop.median_delay_s, scale),
    p90: stop.p90_delay_s === null ? null : place(stop.p90_delay_s, scale),
    worst: stop.max_delay_s === null ? null : place(stop.max_delay_s, scale),
    beyond: stop.max_delay_s !== null && stop.max_delay_s > scale.max,
    tone: toneOf(Math.round(stop.median_delay_s / MINUTE)),
  };
}

/** The scale's minute marks: every minute up to ten, then every two, five or ten. */
export function minuteTicks(scale: Scale): number[] {
  const span = (scale.max - scale.min) / MINUTE;
  const step = span <= 10 ? 1 : span <= 20 ? 2 : span <= 50 ? 5 : 10;
  const ticks: number[] = [];
  for (let m = Math.ceil(scale.min / MINUTE / step) * step; m * MINUTE <= scale.max; m += step) {
    ticks.push(m || 0); // not -0
  }
  return ticks;
}

/** "včas", "+3 min", "−1 min", in whole minutes, to follow "obvykle" or "do". */
export function delayWords(delayS: number): string {
  const words = delayText(Math.round(delayS / MINUTE));
  return words === "Včas" ? "včas" : words;
}
