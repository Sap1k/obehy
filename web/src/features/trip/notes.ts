import type { Trip } from "../../api/client";
import { pictogramOrder, pictogramTier } from "../../components/Pictogram";

// A trip's timetable notes, grouped for reading: when it runs, the codes the timetable marks
// with a sign (JDF fixed codes, CZPTT central notes) in print order, and the longer texts. Of
// the marked codes, the plain ones show as their sign and the stated ones with their text; the
// plain ones' texts and the details are on request. Codes repeat per call range; each is listed once.
// The stopping codes of single calls are the timetable's call flags, not notes.

export interface MarkedNote {
  /** `system:code`, the pictogram's key. */
  key: string;
  text: string;
}

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

const MARKED_SYSTEMS = new Set(["jdf", "czptt_note"]);
// What `call_flags` shows at a call (obehy.api.codes).
const CALL_FLAGS = new Set([
  "request_stop",
  "exit_only",
  "boarding_only",
  "not_stopping",
  "border_control_only",
  "on_request",
  "conditional",
]);

export function noteGroups(trip: Pick<Trip, "features">): NoteGroups {
  const operates = new Map<string, MarkedNote>();
  const marked = new Map<string, MarkedNote>();
  const long = new Set<string>();
  for (const f of trip.features) {
    const source = f.source_code;
    const key = source ? `${source.system}:${source.code}` : null;
    if (key && f.kinds.includes("calendar_designation")) {
      if (!operates.has(key)) operates.set(key, { key, text: f.label.cs });
      continue;
    }
    if (key && source && MARKED_SYSTEMS.has(source.system)) {
      const atCalls = f.scope === "call" || f.scope === "call_range";
      if (atCalls && f.kinds.every((k) => CALL_FLAGS.has(k))) continue;
      if (!marked.has(key)) marked.set(key, { key, text: f.label.cs });
      continue;
    }
    if (f.note?.kind === "czptt_central_note" && f.note.code) {
      // A central note of some calls comes as the note alone: its code is the sign's key.
      const key = `czptt_note:${f.note.code}`;
      const text = f.note.text ?? f.note.label;
      if (text && !marked.has(key)) marked.set(key, { key, text });
      continue;
    }
    if (f.note && !source) {
      const text = f.note.text ?? f.note.label;
      if (text) long.add(text);
    }
  }
  const byOrder = (a: MarkedNote, b: MarkedNote) =>
    pictogramOrder(a.key) - pictogramOrder(b.key) || a.key.localeCompare(b.key);
  const days = [...operates.values()].sort(byOrder);
  const dayTexts = new Set(days.map((d) => d.text));
  const all = [...marked.values()].sort(byOrder);
  const stated = all.filter((n) => pictogramTier(n.key) === "stated");
  return {
    operates: days,
    plain: all.filter((n) => pictogramTier(n.key) === "plain"),
    stated,
    legend: all.filter((n) => !stated.includes(n)),
    detail: all.filter((n) => pictogramTier(n.key) === "detail"),
    long: [...long].filter((t) => !dayTexts.has(t)),
  };
}
