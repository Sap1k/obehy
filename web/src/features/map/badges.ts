import type { Line } from "../../api/client";
import { lineLabel } from "../../components/LineBadge";
import { toneOf, type Tone } from "../../lib/punctuality";

// A vehicle at street zoom is a puck in its line's colours with the line's name, ringed by its
// punctuality; under it, a pointer turns to its bearing. The pucks are drawn into one atlas
// canvas as their lines first appear (at any zoom), so zooming in to pucks draws nothing new;
// deck.gl takes the atlas with its mapping instead of packing an image per icon.

/** The puck's diameter on screen, in CSS pixels. */
export const PUCK_PX = 26;
/** The icons' heights on screen: the puck with its ring, and the pointer around it. */
export const PUCK_ICON_PX = PUCK_PX + 4;
export const ARROW_ICON_PX = PUCK_PX + 16;

const SCALE = 2;
const CELL = ARROW_ICON_PX * SCALE;
const COLUMNS = 16;
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

let atlas: Atlas | null = null;

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

/** The atlas with every key in `keys`: the same object when it already had them all. */
export function atlasWith(keys: Iterable<string>): Atlas {
  const missing = new Set<string>();
  for (const k of keys) if (!atlas?.mapping[k]) missing.add(k);
  if (atlas && missing.size === 0) return atlas;

  const mapping: Record<string, IconFrame> = { ...atlas?.mapping };
  if (!mapping[ARROW_KEY]) missing.add(ARROW_KEY);
  const count = Object.keys(mapping).length + missing.size;
  const rows = Math.ceil(count / COLUMNS);
  const canvas = document.createElement("canvas");
  canvas.width = COLUMNS * CELL;
  canvas.height = rows * CELL;
  const g = canvas.getContext("2d")!;
  if (atlas) g.drawImage(atlas.canvas, 0, 0);

  let slot = Object.keys(mapping).length;
  for (const key of missing) {
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
