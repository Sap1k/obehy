import type { Schemas } from "../api/client";
import { delayText, toneOf, type Tone } from "./punctuality";

// How a journey's delay history (`/trips/{id}/history`) reads. The API measures it, per day
// and over the days, between the stops the rider picks; here it is only worded and coloured.

export type TripHistory = Schemas["TripHistory"];
export type HistoryDay = Schemas["HistoryDay"];
export type HistoryCall = Schemas["HistoryCall"];
export type HistoryDelay = Schemas["HistoryDelay"];
export type HistorySegmentRun = Schemas["HistorySegmentRun"];

const MINUTE = 60;

/** The tone of a delay in seconds, by the boards' bands. */
export function delayTone(delayS: number | null): Tone {
  return delayS === null ? "unknown" : toneOf(Math.round(delayS / MINUTE));
}

/** "včas", "+3 min", "−1 min", in whole minutes, to follow "obvykle" or stand alone. */
export function delayWords(delayS: number): string {
  const words = delayText(Math.round(delayS / MINUTE));
  return words === "Včas" ? "včas" : words;
}
