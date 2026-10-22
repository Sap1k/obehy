import type { Trip } from "../../api/client";

// A trip's timetable notes, grouped for reading: when it runs, the train's short notes (CZPTT
// central notes have a short label: "kolo", "WiFi", "1+2"), and the longer texts. Notes repeat
// per call range; each is listed once.

export interface ShortNote {
  label: string;
  /** The full wording, when the label shortens it. */
  text: string | null;
}

export interface NoteGroups {
  operates: string[];
  short: ShortNote[];
  long: string[];
}

export function noteGroups(trip: Pick<Trip, "amenities" | "notes">): NoteGroups {
  const operates = [...new Set(trip.amenities.operates.map((l) => l.cs))];
  const short = new Map<string, ShortNote>();
  const long = new Set<string>();
  for (const n of trip.notes) {
    if (n.kind === "czptt_central_note" && n.label) {
      const key = n.code ?? n.label;
      if (!short.has(key)) short.set(key, { label: n.label, text: n.text });
      continue;
    }
    const text = n.text ?? n.label;
    if (text && !operates.includes(text)) long.add(text);
  }
  return { operates, short: [...short.values()], long: [...long] };
}
