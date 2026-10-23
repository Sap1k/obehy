import { EASE_IN_OUT, EASE_OUT } from "../../lib/motion";

// How the timetable's hidden ranges open and close. The calls behind "N more stops" unfold out
// of the expander's own height and fold back into it, so the rows below move smoothly and land
// where they were; the earlier calls (above the anchor, which stays still) fade in upwards from
// the anchor, and fade out. Rows are the list's `[data-range]` items.

const UNFOLD_MS = 280;
const FOLD_MS = 200;
const FADE_MS = 180;
/** Each earlier call fades in this much after the one below it, up to STAGGER_MAX. */
const STAGGER_MS = 18;
const STAGGER_MAX = 180;

/** Heights from `fromTotal` (all rows together) to each row's own, or back. Row heights are
 * shared out in proportion, so the group grows from (or shrinks to) exactly `fromTotal`. */
function heights(rows: HTMLElement[], total: number): [number, number][] {
  const own = rows.map((r) => r.offsetHeight);
  const sum = own.reduce((a, b) => a + b, 0) || 1;
  return own.map((h) => [(total * h) / sum, h]);
}

function clipWhile(row: HTMLElement, run: Animation): Animation {
  const was = row.style.overflow;
  row.style.overflow = "hidden";
  void run.finished.finally(() => {
    row.style.overflow = was;
  });
  return run;
}

/** Rows a range just added: middle calls unfold from the expander's height, earlier calls
 * fade in from the anchor upwards. */
export function unfold(added: HTMLElement[], expanderPx: number): void {
  const middle = added.filter((r) => r.dataset.range === "middle");
  heights(middle, expanderPx).forEach(([from, to], i) => {
    const row = middle[i]!;
    clipWhile(
      row,
      row.animate(
        [
          { height: `${from}px`, minHeight: "0px", opacity: 0 },
          { height: `${to}px`, minHeight: "0px", opacity: 1 },
        ],
        { duration: UNFOLD_MS, easing: EASE_OUT },
      ),
    );
  });
  const before = added.filter((r) => r.dataset.range === "before");
  before.forEach((row, i) => {
    const fromAnchor = before.length - 1 - i;
    row.animate(
      [
        { opacity: 0, transform: "translateY(4px)" },
        { opacity: 1, transform: "none" },
      ],
      {
        duration: FADE_MS,
        delay: Math.min(fromAnchor * STAGGER_MS, STAGGER_MAX),
        easing: EASE_OUT,
        fill: "backwards",
      },
    );
  });
}

/** Rows a range is about to take away: middle calls fold into the expander's height, earlier
 * calls fade out. Resolves when they are done (they stay folded until removed). */
export function foldAway(leaving: HTMLElement[], expanderPx: number): Promise<unknown> {
  const middle = leaving.filter((r) => r.dataset.range === "middle");
  const runs = heights(middle, expanderPx).map(([to, from], i) => {
    const row = middle[i]!;
    return clipWhile(
      row,
      row.animate(
        [
          { height: `${from}px`, minHeight: "0px", opacity: 1 },
          { height: `${to}px`, minHeight: "0px", opacity: 0 },
        ],
        { duration: FOLD_MS, easing: EASE_IN_OUT, fill: "forwards" },
      ),
    );
  });
  for (const row of leaving.filter((r) => r.dataset.range === "before")) {
    runs.push(
      row.animate([{ opacity: 1 }, { opacity: 0 }], {
        duration: FADE_MS,
        easing: EASE_IN_OUT,
        fill: "forwards",
      }),
    );
  }
  // A page that is not drawing (a hidden tab) never finishes them: the range closes anyway.
  const late = new Promise((done) => window.setTimeout(done, Math.max(FOLD_MS, FADE_MS) + 150));
  return Promise.race([Promise.all(runs.map((r) => r.finished.catch(() => undefined))), late]);
}
