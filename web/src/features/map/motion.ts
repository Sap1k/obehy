// Vehicles glide from where they were drawn to their new position over one second after each
// answer, keyed by vehicle ID (deck.gl's own transitions go by array index, so a vehicle could
// glide in from where another one left). A vehicle that is new, or jumped further than a
// vehicle moves between answers, appears in place.

export const GLIDE_MS = 1000;

/** Further than this (in degrees, about 2 km) is a jump, not a move. */
const JUMP_DEG = 0.02;

export type Position = readonly [number, number];

export interface Glide {
  ids: string[];
  from: Float64Array;
  to: Float64Array;
  startedAt: number;
}

/** A glide from the positions drawn now (`drawn`, by vehicle ID) to `targets`. */
export function startGlide(
  drawn: ReadonlyMap<string, Position>,
  targets: readonly { id: string; position: Position }[],
  now: number,
): Glide {
  const n = targets.length;
  const from = new Float64Array(n * 2);
  const to = new Float64Array(n * 2);
  targets.forEach(({ id, position }, i) => {
    const was = drawn.get(id);
    const near =
      was !== undefined &&
      Math.abs(was[0] - position[0]) < JUMP_DEG &&
      Math.abs(was[1] - position[1]) < JUMP_DEG;
    const start = near ? was : position;
    from[i * 2] = start[0];
    from[i * 2 + 1] = start[1];
    to[i * 2] = position[0];
    to[i * 2 + 1] = position[1];
  });
  return { ids: targets.map((t) => t.id), from, to, startedAt: now };
}

/** How far along the glide is at `now`, 0…1 (linear, as the camera that follows a vehicle). */
export function progress(glide: Glide, now: number): number {
  return Math.min(1, Math.max(0, (now - glide.startedAt) / GLIDE_MS));
}

/** The positions at `t` (0…1), written into `out` as [lon, lat, lon, lat, …]. */
export function positionsAt(glide: Glide, t: number, out: Float64Array): Float64Array {
  for (let i = 0; i < glide.from.length; i++) {
    out[i] = glide.from[i]! + (glide.to[i]! - glide.from[i]!) * t;
  }
  return out;
}

/** The drawn positions by vehicle ID, the start of the next glide. */
export function drawnPositions(glide: Glide, positions: Float64Array): Map<string, Position> {
  const out = new Map<string, Position>();
  glide.ids.forEach((id, i) => out.set(id, [positions[i * 2]!, positions[i * 2 + 1]!]));
  return out;
}
