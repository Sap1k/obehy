// The map asks for vehicles by fixed cells, not by its viewport: every viewer asks for the same
// URLs (so nginx's micro-cache serves them all), and a cell stays well inside the API's largest
// bbox (api-v1.toml `vehicles.bbox_max_deg2`). A cell answering the API's most vehicles is cut
// into quarters, down to a quarter degree.

export interface Bounds {
  west: number;
  south: number;
  east: number;
  north: number;
}

/** Czechia with a margin: the map's reach and the cells' clamp. */
export const AREA: Bounds = { west: 11.8, south: 48.4, east: 19.1, north: 51.2 };

/** The API's most vehicles in one answer (api-v1.toml `vehicles.limit`). */
export const VEHICLES_LIMIT = 3000;

// Cells are counted in quarter degrees, so their edges print exactly (14.25, not 14.249999).
const BASE = 4;
const SMALLEST = 1;

export interface Cell {
  /** The bbox parameter, "minlon,minlat,maxlon,maxlat". */
  key: string;
  x: number;
  y: number;
  size: number;
}

function cell(x: number, y: number, size: number): Cell {
  const q = (n: number) => String(n / 4);
  return { key: `${q(x)},${q(y)},${q(x + size)},${q(y + size)}`, x, y, size };
}

/** A cell's four quarters, or none when it is the smallest. */
export function quarters(c: Cell): Cell[] {
  if (c.size <= SMALLEST) return [];
  const h = c.size / 2;
  return [
    cell(c.x, c.y, h),
    cell(c.x + h, c.y, h),
    cell(c.x, c.y + h, h),
    cell(c.x + h, c.y + h, h),
  ];
}

/** The cells covering the part of `bounds` inside AREA; a cell `split` says so of is replaced
 * by its quarters (and theirs, when also split). */
export function cellsFor(bounds: Bounds, split: (key: string) => boolean = () => false): Cell[] {
  const west = Math.max(bounds.west, AREA.west);
  const east = Math.min(bounds.east, AREA.east);
  const south = Math.max(bounds.south, AREA.south);
  const north = Math.min(bounds.north, AREA.north);
  if (west >= east || south >= north) return [];
  const out: Cell[] = [];
  const add = (c: Cell) => {
    const parts = split(c.key) ? quarters(c) : [];
    if (parts.length === 0) out.push(c);
    else for (const p of parts) if (overlaps(p, west, south, east, north)) add(p);
  };
  for (let y = Math.floor(south); y < north; y++) {
    for (let x = Math.floor(west); x < east; x++) add(cell(x * BASE, y * BASE, BASE));
  }
  return out;
}

function overlaps(c: Cell, west: number, south: number, east: number, north: number): boolean {
  return (
    c.x / 4 < east && (c.x + c.size) / 4 > west && c.y / 4 < north && (c.y + c.size) / 4 > south
  );
}
