import type { FeatureCollection, Geometry } from "geojson";

// The open trip on the map, drawn by MapLibre itself (style.ts `trip*` layers): its path with
// direction arrows, and its stops at their posts with their names. MapLibre smooths its lines
// and places the names around the stops without overlapping each other or the base map's
// labels, the more important stops first.

export interface TripStop {
  /** The stop place, whose board a click opens. */
  id: string;
  position: [number, number];
  name: string;
  passed: boolean;
  /** Which names win where they would overlap: lower first. */
  rank: number;
}

export interface TripOverlay {
  /** The path's lines, [lon, lat] each. */
  paths: [number, number][][];
  stops: TripStop[];
  colour: string;
}

/** Name ranks: the board's stop and the next stop, then the ends, then the rest. */
export const RANK = { focus: 0, end: 1, other: 2 } as const;

/** A trip mostly in one town (more than this share of its stops) is that town's line. */
const HOME_TOWN_SHARE = 0.5;

/** JDF names read "Town,Part,Place" ("Praha,Můstek", "Nový Bor,,nám."). When more than half of
 * the trip's stops are in one town, that town is left out of their names ("Můstek"), the rest
 * starting with a capital ("Nám."); other towns' stops keep the whole name ("Kolín, Zálabí").
 * Names without a comma (trains, villages) stay as they are. */
export function stopLabels(names: readonly string[]): string[] {
  const parts = names.map((n) =>
    n
      .split(",")
      .map((p) => p.trim())
      .filter(Boolean),
  );
  const counts = new Map<string, number>();
  for (const p of parts) if (p.length > 1) counts.set(p[0]!, (counts.get(p[0]!) ?? 0) + 1);
  let home: string | undefined;
  for (const [town, n] of counts) if (n > names.length * HOME_TOWN_SHARE) home = town;
  return parts.map((p, i) => {
    if (p.length <= 1) return p[0] ?? names[i]!;
    if (p[0] !== home) return p.join(", ");
    const rest = p.slice(1).join(", ");
    return rest.charAt(0).toLocaleUpperCase("cs") + rest.slice(1);
  });
}

export function tripFeatures(trip: TripOverlay | null): FeatureCollection<Geometry> {
  if (!trip) return { type: "FeatureCollection", features: [] };
  return {
    type: "FeatureCollection",
    features: [
      {
        type: "Feature",
        geometry: { type: "MultiLineString", coordinates: trip.paths },
        properties: { kind: "path", colour: trip.colour },
      },
      ...trip.stops.map((s) => ({
        type: "Feature" as const,
        geometry: { type: "Point" as const, coordinates: s.position },
        properties: {
          kind: "stop",
          id: s.id,
          colour: trip.colour,
          name: s.name,
          passed: s.passed,
          rank: s.rank,
        },
      })),
    ],
  };
}

/** The arrows along the path: a white chevron pointing along the line, outlined dark so it
 * reads on any line colour. Drawn at twice its size (pixelRatio 2). */
export function arrowImage(): ImageData {
  const size = 48;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  ctx.lineCap = "round";
  ctx.lineJoin = "round";
  const chevron = () => {
    ctx.beginPath();
    ctx.moveTo(17, 12);
    ctx.lineTo(30, 24);
    ctx.lineTo(17, 36);
    ctx.stroke();
  };
  ctx.strokeStyle = "rgba(0, 0, 0, 0.45)";
  ctx.lineWidth = 11;
  chevron();
  ctx.strokeStyle = "#ffffff";
  ctx.lineWidth = 6;
  chevron();
  return ctx.getImageData(0, 0, size, size);
}
