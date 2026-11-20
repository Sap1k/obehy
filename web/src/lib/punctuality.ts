import type { Schemas } from "../api/client";

// How a time reads on a board or in a timetable (Figma 08, "Departure time"): the expected
// time, coloured by punctuality, with the delay in words under it. The API decides the delay
// (shown minutes) and its band (`api/punctuality.py`); here it is only coloured and worded.

export type CallStatus =
  "scheduled" | "predicted" | "actual" | "inferred" | "no_realtime" | "cancelled";

export type Punctuality = Schemas["Departure"]["punctuality"];

export type Tone = "early" | "onTime" | "late" | "veryLate" | "cancelled" | "unknown";

export interface TimeState {
  /** The time shown: the expected one when known, else the scheduled one. */
  shown: string;
  scheduled: string;
  tone: Tone;
  /** "Včas", "+4 min", "−2 min", "Zrušeno", "Dle JŘ", "≈ +4 min". */
  caption: string;
  delayMin: number | null;
  struck: boolean;
}

const TONES: Record<Punctuality, Tone> = {
  early: "early",
  on_time: "onTime",
  late: "late",
  very_late: "veryLate",
  unknown: "unknown",
  cancelled: "cancelled",
};

/** The colour of the API's band. */
export function toneOf(punctuality: Punctuality): Tone {
  return TONES[punctuality];
}

export function delayText(delayMin: number): string {
  if (delayMin === 0) return "Včas";
  return delayMin > 0 ? `+${delayMin} min` : `−${-delayMin} min`;
}

/** A time as the API judged it: `delayMin` and `punctuality` are the row's or call's own.
 * `offline` (the page's data could not be refreshed) shows it as approximate, as a stale one. */
export function timeState(
  scheduled: string,
  estimated: string | null | undefined,
  delayMin: number | null,
  punctuality: Punctuality,
  offline = false,
): TimeState {
  if (punctuality === "cancelled") {
    return {
      shown: scheduled,
      scheduled,
      tone: "cancelled",
      caption: "Zrušeno",
      delayMin: null,
      struck: true,
    };
  }
  if (delayMin === null || !estimated) {
    return {
      shown: scheduled,
      scheduled,
      tone: "unknown",
      caption: "Dle JŘ",
      delayMin: null,
      struck: false,
    };
  }
  const words = delayText(delayMin);
  if (offline || punctuality === "unknown") {
    const caption = `≈ ${delayMin === 0 ? "včas" : words}`;
    return { shown: estimated, scheduled, tone: "unknown", caption, delayMin, struck: false };
  }
  return {
    shown: estimated,
    scheduled,
    tone: toneOf(punctuality),
    caption: words,
    delayMin,
    struck: false,
  };
}
