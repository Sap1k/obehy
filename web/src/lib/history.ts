import type { Schemas } from "../api/client";
import { delayText } from "./punctuality";

// How a journey's delay history (`/trips/{id}/history`) reads. The API measures it, per day
// and over the days, between the stops the rider picks; here it is only worded and coloured.

export type TripHistory = Schemas["TripHistory"];
export type HistoryDay = Schemas["HistoryDay"];
export type HistoryCall = Schemas["HistoryCall"];
export type HistoryDelay = Schemas["HistoryDelay"];
export type HistorySegmentRun = Schemas["HistorySegmentRun"];

/** "včas", "+3 min", "−1 min" (the API's shown minutes), to follow "obvykle" or stand alone. */
export function delayWords(delayMin: number): string {
  const words = delayText(delayMin);
  return words === "Včas" ? "včas" : words;
}
