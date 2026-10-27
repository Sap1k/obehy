import type { Line } from "../../api/client";
import { lineLabel } from "../../components/LineBadge";
import { toneOf, type Tone } from "../../lib/punctuality";

// A vehicle at street zoom is a puck in its line's colours with the line's name, ringed by its
// punctuality; under it, a pointer turns to its bearing. The pucks are drawn into one atlas
// canvas as they are first shown; deck.gl takes the atlas with its mapping instead of packing an
// image per icon. The atlas stays within a texture every GPU takes: one larger than the GPU's
// limit is not made at all, and every icon on it samples black (Xclipse allows 8192 px).

/** The puck's diameter on screen, in CSS pixels. */
export const PUCK_PX = 26;
/** The icons' heights on screen: the puck with its ring, and the pointer around it. */
export const PUCK_ICON_PX = PUCK_PX + 4;
export const ARROW_ICON_PX = PUCK_PX + 16;

const SCALE = 2;
const CELL = ARROW_ICON_PX * SCALE;
/** The atlas's largest side. */
const MAX_PX = 4096;
const COLUMNS = Math.floor(MAX_PX / CELL);
const CAPACITY = COLUMNS * Math.floor(MAX_PX / CELL);
const FONT = "'Manrope Variable', system-ui, sans-serif";

/** The ring's colours: the board's punctuality bands, made to stand out on a map. */
const RING: Record<Tone, string> = {
  early: "#2f6fd6",
  onTime: "#2e8a55",
  late: "#e0a100",
  veryLate: "#d9480f",
  cancelled: "#af3038",
  unknown: "#ffffff",
};

const INK = "#153438";
const WHITE = "#ffffff";

export function hexColour(colour: string | null | undefined, fallback: string): string {
  if (!colour) return fallback;
  return colour.startsWith("#") ? colour : `#${colour}`;
}

export function rgb(hex: string): [number, number, number] {
  const n = Number.parseInt(hex.slice(1), 16);
  return [(n >> 16) & 255, (n >> 8) & 255, n & 255];
}

/** The punctuality band of a vehicle's delay, in whole minutes as the boards round it. */
export function toneOfDelay(delayS: number | null | undefined): Tone {
  return delayS === null || delayS === undefined ? "unknown" : toneOf(Math.round(delayS / 60));
}

export interface PuckSpec {
  line: Line | null;
  delay_s: number | null;
}

/** The atlas key of a vehicle's puck. */
export function puckKey(v: PuckSpec): string {
  const label = v.line ? lineLabel(v.line) : "?";
  return `${label}|${hexColour(v.line?.color, INK)}|${hexColour(v.line?.text_color, WHITE)}|${toneOfDelay(v.delay_s)}`;
}

export const ARROW_KEY = "arrow";

export interface IconFrame {
  x: number;
  y: number;
  width: number;
  height: number;
  mask: boolean;
}

/** The atlas: a canvas (replaced, not redrawn, when it gains icons, so deck.gl uploads it
 * again) and where each icon is on it. */
export interface Atlas {
  canvas: HTMLCanvasElement;
  mapping: Record<string, IconFrame>;
}

// A phone's browser wipes a canvas kept on the GPU when it drops the GPU's context (a page in
// the background): the atlas's pixels cannot be trusted past that. So a new atlas draws every
// icon anew (never copies the old canvas), and one whose canvas lost its context, or was kept
// past a map rebuilt (resetAtlas), is dropped and drawn again.
let atlas: Atlas | null = null;

/** Draw the atlas again on its next use (the map was rebuilt after losing its context). */
export function resetAtlas(): void {
  atlas = null;
}

function newCanvas(width: number, height: number): HTMLCanvasElement {
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const drop = () => {
    if (atlas?.canvas === canvas) atlas = null;
  };
  canvas.addEventListener("contextlost", drop);
  canvas.addEventListener("contextrestored", drop);
  return canvas;
}

function drawArrow(g: CanvasRenderingContext2D, x: number, y: number) {
  const mid = CELL / 2;
  const r = (PUCK_PX / 2) * SCALE;
  g.fillStyle = WHITE;
  g.beginPath();
  g.arc(x + mid, y + mid, r, 0, Math.PI * 2);
  g.fill();
  g.beginPath();
  g.moveTo(x + mid, y + SCALE);
  g.lineTo(x + mid + 7 * SCALE, y + mid - r + 4 * SCALE);
  g.lineTo(x + mid - 7 * SCALE, y + mid - r + 4 * SCALE);
  g.closePath();
  g.fill();
}

function drawPuck(g: CanvasRenderingContext2D, key: string, x: number, y: number) {
  const [label = "?", fill = INK, text = WHITE, tone = "unknown"] = key.split("|");
  const size = PUCK_ICON_PX * SCALE;
  const mid = size / 2;
  const r = (PUCK_PX / 2) * SCALE;
  g.beginPath();
  g.arc(x + mid, y + mid, r, 0, Math.PI * 2);
  g.fillStyle = RING[tone as Tone] ?? WHITE;
  g.fill();
  g.beginPath();
  g.arc(x + mid, y + mid, r - 2.5 * SCALE, 0, Math.PI * 2);
  g.fillStyle = fill;
  g.fill();
  const shown = label.length > 5 ? label.slice(0, 5) : label;
  const px = [24, 24, 24, 19, 15, 12][shown.length] ?? 12;
  g.font = `700 ${px}px ${FONT}`;
  g.fillStyle = text;
  g.textAlign = "center";
  g.textBaseline = "middle";
  g.fillText(shown, x + mid, y + mid + 1);
}

/** The atlas with every key in `keys`: the same object when it already had them all. A full
 * atlas starts over with only `keys` (as many as fit). */
export function atlasWith(keys: Iterable<string>): Atlas {
  const wanted = new Set(keys);
  const missing = [...wanted].filter((k) => !atlas?.mapping[k]);
  if (atlas && missing.length === 0) return atlas;

  const kept =
    atlas && Object.keys(atlas.mapping).length + missing.length <= CAPACITY ? atlas : null;
  // The arrow first, so it is never left out; the icons kept stay where they were.
  const drawn = [
    ...new Set([ARROW_KEY, ...(kept ? [...Object.keys(kept.mapping), ...missing] : wanted)]),
  ].slice(0, CAPACITY);
  const mapping: Record<string, IconFrame> = {};
  const canvas = newCanvas(COLUMNS * CELL, Math.ceil(drawn.length / COLUMNS) * CELL);
  const g = canvas.getContext("2d")!;

  let slot = 0;
  for (const key of drawn) {
    const x = (slot % COLUMNS) * CELL;
    const y = Math.floor(slot / COLUMNS) * CELL;
    slot += 1;
    if (key === ARROW_KEY) {
      drawArrow(g, x, y);
      mapping[key] = { x, y, width: CELL, height: CELL, mask: true };
    } else {
      drawPuck(g, key, x, y);
      const size = PUCK_ICON_PX * SCALE;
      mapping[key] = { x, y, width: size, height: size, mask: false };
    }
  }
  atlas = { canvas, mapping };
  return atlas;
}
