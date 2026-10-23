// The phone's bottom sheet over the map: peek (the header), half (45 % of the map) or full (70 %,
// so the map always shows above it). The sheet draws itself at these heights and the map keeps
// what it centres clear of them, both from the same numbers.

export type SheetSnap = "peek" | "half" | "full";

export const SNAPS: readonly SheetSnap[] = ["peek", "half", "full"];

const PEEK_PX = 148;
const HALF = 0.45;
const FULL = 0.7;

/** Each snap's visible height on a map `hostPx` tall. */
export function sheetHeights(hostPx: number): Record<SheetSnap, number> {
  const full = Math.max(PEEK_PX, Math.round(hostPx * FULL));
  return {
    peek: Math.min(PEEK_PX, full),
    half: Math.min(full, Math.max(PEEK_PX, Math.round(hostPx * HALF))),
    full,
  };
}
