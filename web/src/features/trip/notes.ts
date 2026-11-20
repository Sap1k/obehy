import type { Schemas, Trip } from "../../api/client";

// A trip's timetable notes as the API groups them (`codes.note_groups`): when it runs, the
// codes the timetable marks with a sign in print order, and the longer texts. Here only how
// they are laid out: the plain signs as badges, the stated ones with their text, the plain
// ones' texts and the details on request.

export type MarkedNote = Schemas["MarkedNote"];

export interface NoteGroups {
  operates: MarkedNote[];
  plain: MarkedNote[];
  stated: MarkedNote[];
  /** On request: the plain codes' texts and the details, in print order. */
  legend: MarkedNote[];
  /** The low-priority codes, hinted at by their signs alone until the legend opens. */
  detail: MarkedNote[];
  long: string[];
}

export function noteGroups(trip: Pick<Trip, "notes">): NoteGroups {
  const { operates, marked, long } = trip.notes;
  return {
    operates,
    plain: marked.filter((n) => n.tier === "plain"),
    stated: marked.filter((n) => n.tier === "stated"),
    legend: marked.filter((n) => n.tier !== "stated"),
    detail: marked.filter((n) => n.tier === "detail"),
    long,
  };
}
