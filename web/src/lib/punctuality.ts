import { minutesBetween } from "./time";

// How a time reads on a board or in a timetable (Figma 08, "Departure time"): the expected
// time, coloured by punctuality, with the delay in words under it.

export type CallStatus =
  "scheduled" | "predicted" | "actual" | "inferred" | "no_realtime" | "cancelled";

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

/** Bands in whole minutes of the shown times: early ≤ −2, on time −1…+2, late +3…+9, very
 * late ≥ +10. */
export function toneOf(delayMin: number): Tone {
  if (delayMin <= -2) return "early";
  if (delayMin <= 2) return "onTime";
  if (delayMin <= 9) return "late";
  return "veryLate";
}

export function delayText(delayMin: number): string {
  if (delayMin === 0) return "Včas";
  return delayMin > 0 ? `+${delayMin} min` : `−${-delayMin} min`;
}

const LIVE: ReadonlySet<string> = new Set(["predicted", "actual", "inferred"]);

export function timeState(
  scheduled: string,
  estimated: string | null | undefined,
  status: string,
  stale: boolean | null | undefined,
): TimeState {
  if (status === "cancelled") {
    return {
      shown: scheduled,
      scheduled,
      tone: "cancelled",
      caption: "Zrušeno",
      delayMin: null,
      struck: true,
    };
  }
  if (!estimated || !LIVE.has(status)) {
    return {
      shown: scheduled,
      scheduled,
      tone: "unknown",
      caption: "Dle JŘ",
      delayMin: null,
      struck: false,
    };
  }
  const delayMin = minutesBetween(scheduled, estimated);
  const words = delayText(delayMin);
  if (stale) {
    return {
      shown: estimated,
      scheduled,
      tone: "unknown",
      caption: `≈ ${delayMin === 0 ? "včas" : words}`,
      delayMin,
      struck: false,
    };
  }
  return {
    shown: estimated,
    scheduled,
    tone: toneOf(delayMin),
    caption: words,
    delayMin,
    struck: false,
  };
}
