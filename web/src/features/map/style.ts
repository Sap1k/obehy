import type { ExpressionSpecification, LayerSpecification, StyleSpecification } from "maplibre-gl";

// The base map: OpenStreetMap vector tiles in the OpenMapTiles schema from OpenFreeMap's
// public host (no key), in our own quiet style: land, water and roads stay in the background,
// railways read clearly, and the vehicles on top carry the colour. Another OpenMapTiles host can
// replace TILES and GLYPHS.

export const TILES = "https://tiles.openfreemap.org/planet";
export const GLYPHS = "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf";

const ATTRIBUTION =
  '<a href="https://openfreemap.org" target="_blank">OpenFreeMap</a> ' +
  '<a href="https://www.openmaptiles.org/" target="_blank">© OpenMapTiles</a> ' +
  '<a href="https://www.openstreetmap.org/copyright" target="_blank">© OpenStreetMap</a>';

export interface Palette {
  land: string;
  wood: string;
  grass: string;
  residential: string;
  park: string;
  water: string;
  building: string;
  road: string;
  roadMajor: string;
  motorway: string;
  casing: string;
  rail: string;
  tram: string;
  boundary: string;
  label: string;
  labelMinor: string;
  labelWater: string;
  halo: string;
}

/** From the Figma 07 tokens (src/theme/tokens.css): the background, borders and text. */
export const LIGHT: Palette = {
  land: "#f4f7ef",
  wood: "#dfe9d4",
  grass: "#e7efdc",
  residential: "#eef2e8",
  park: "#e0ebd6",
  water: "#cddfe3",
  building: "#e6eae2",
  road: "#ffffff",
  roadMajor: "#ffffff",
  motorway: "#fbfcf8",
  casing: "#d4dfd7",
  rail: "#7d8e87",
  tram: "#9aa9a2",
  boundary: "#9aa9a2",
  label: "#153438",
  labelMinor: "#526760",
  labelWater: "#4e7d8a",
  halo: "#f4f7ef",
};

export const DARK: Palette = {
  land: "#102b2d",
  wood: "#12322f",
  grass: "#133330",
  residential: "#13302f",
  park: "#143531",
  water: "#0a2124",
  building: "#18383a",
  road: "#1d3f41",
  roadMajor: "#234a4c",
  motorway: "#2a5355",
  casing: "#0e2628",
  rail: "#5d7a75",
  tram: "#4f6b67",
  boundary: "#4f6b67",
  label: "#f4f7ef",
  labelMinor: "#b6c8bd",
  labelWater: "#6f9ba6",
  halo: "#102b2d",
};

const NAME: ExpressionSpecification = ["coalesce", ["get", "name:cs"], ["get", "name"]];
const REGULAR = ["Noto Sans Regular"];
const BOLD = ["Noto Sans Bold"];
const ITALIC = ["Noto Sans Italic"];

const MINOR = ["minor", "service", "track"];
const MAJOR = ["primary", "secondary", "tertiary"];
const MOTORWAY = ["motorway", "trunk"];

type Width = ExpressionSpecification;
/** A width growing exponentially with zoom between two stops. */
const grow = (z0: number, w0: number, z1: number, w1: number): Width => [
  "interpolate",
  ["exponential", 1.5],
  ["zoom"],
  z0,
  w0,
  z1,
  w1,
];

function layers(p: Palette): LayerSpecification[] {
  const src = { source: "osm" } as const;
  const roads = (
    id: string,
    classes: string[],
    minzoom: number,
    colour: string,
    width: Width,
  ): LayerSpecification => ({
    id,
    type: "line",
    ...src,
    "source-layer": "transportation",
    minzoom,
    filter: [
      "all",
      ["match", ["get", "class"], classes, true, false],
      ["!=", ["get", "brunnel"], "tunnel"],
    ],
    layout: { "line-cap": "round", "line-join": "round" },
    paint: { "line-color": colour, "line-width": width },
  });
  return [
    { id: "land", type: "background", paint: { "background-color": p.land } },
    {
      id: "landcover-wood",
      type: "fill",
      ...src,
      "source-layer": "landcover",
      filter: ["==", ["get", "class"], "wood"],
      paint: { "fill-color": p.wood },
    },
    {
      id: "landcover-grass",
      type: "fill",
      ...src,
      "source-layer": "landcover",
      filter: ["match", ["get", "class"], ["grass", "wetland"], true, false],
      paint: { "fill-color": p.grass },
    },
    {
      id: "landuse-residential",
      type: "fill",
      ...src,
      "source-layer": "landuse",
      filter: [
        "match",
        ["get", "class"],
        ["residential", "suburb", "neighbourhood", "commercial", "industrial", "retail"],
        true,
        false,
      ],
      paint: { "fill-color": p.residential },
    },
    {
      id: "park",
      type: "fill",
      ...src,
      "source-layer": "park",
      paint: { "fill-color": p.park },
    },
    {
      id: "water",
      type: "fill",
      ...src,
      "source-layer": "water",
      filter: ["!=", ["get", "brunnel"], "tunnel"],
      paint: { "fill-color": p.water },
    },
    {
      id: "waterway",
      type: "line",
      ...src,
      "source-layer": "waterway",
      filter: ["!=", ["get", "brunnel"], "tunnel"],
      paint: { "line-color": p.water, "line-width": grow(8, 0.5, 16, 4) },
    },
    {
      id: "building",
      type: "fill",
      ...src,
      "source-layer": "building",
      minzoom: 14,
      paint: { "fill-color": p.building },
    },
    {
      id: "boundary-country",
      type: "line",
      ...src,
      "source-layer": "boundary",
      filter: ["all", ["==", ["get", "admin_level"], 2], ["!=", ["get", "maritime"], 1]],
      paint: {
        "line-color": p.boundary,
        "line-width": grow(4, 0.8, 12, 2),
        "line-dasharray": [3, 2],
      },
    },
    {
      id: "boundary-region",
      type: "line",
      ...src,
      "source-layer": "boundary",
      minzoom: 7,
      filter: ["==", ["get", "admin_level"], 4],
      paint: { "line-color": p.boundary, "line-width": 0.6, "line-dasharray": [2, 2] },
    },
    roads("road-minor-casing", MINOR, 13, p.casing, grow(13, 2, 18, 14)),
    roads("road-major-casing", MAJOR, 8, p.casing, grow(8, 1.5, 18, 22)),
    roads("road-motorway-casing", MOTORWAY, 6, p.casing, grow(6, 1.5, 18, 26)),
    roads("road-minor", MINOR, 13, p.road, grow(13, 1, 18, 11)),
    roads("road-major", MAJOR, 8, p.roadMajor, grow(8, 0.6, 18, 18)),
    roads("road-motorway", MOTORWAY, 6, p.motorway, grow(6, 0.8, 18, 22)),
    {
      id: "tram",
      type: "line",
      ...src,
      "source-layer": "transportation",
      minzoom: 12,
      filter: ["all", ["==", ["get", "class"], "transit"], ["!=", ["get", "brunnel"], "tunnel"]],
      paint: { "line-color": p.tram, "line-width": grow(12, 0.8, 18, 2.5) },
    },
    {
      id: "rail",
      type: "line",
      ...src,
      "source-layer": "transportation",
      minzoom: 6,
      filter: [
        "all",
        ["==", ["get", "class"], "rail"],
        ["!=", ["get", "brunnel"], "tunnel"],
        ["!", ["has", "service"]],
      ],
      paint: { "line-color": p.rail, "line-width": grow(6, 0.6, 18, 3) },
    },
    {
      id: "rail-service",
      type: "line",
      ...src,
      "source-layer": "transportation",
      minzoom: 13,
      filter: ["all", ["==", ["get", "class"], "rail"], ["has", "service"]],
      paint: { "line-color": p.rail, "line-width": grow(13, 0.5, 18, 1.5), "line-opacity": 0.6 },
    },
    {
      id: "road-name",
      type: "symbol",
      ...src,
      "source-layer": "transportation_name",
      minzoom: 14,
      layout: {
        "symbol-placement": "line",
        "text-field": NAME,
        "text-font": REGULAR,
        "text-size": 11,
      },
      paint: { "text-color": p.labelMinor, "text-halo-color": p.halo, "text-halo-width": 1.5 },
    },
    {
      id: "water-name",
      type: "symbol",
      ...src,
      "source-layer": "water_name",
      layout: { "text-field": NAME, "text-font": ITALIC, "text-size": 12 },
      paint: { "text-color": p.labelWater, "text-halo-color": p.halo, "text-halo-width": 1.5 },
    },
    {
      id: "place-minor",
      type: "symbol",
      ...src,
      "source-layer": "place",
      minzoom: 11,
      filter: [
        "match",
        ["get", "class"],
        ["village", "hamlet", "suburb", "quarter", "neighbourhood"],
        true,
        false,
      ],
      layout: { "text-field": NAME, "text-font": REGULAR, "text-size": 12 },
      paint: { "text-color": p.labelMinor, "text-halo-color": p.halo, "text-halo-width": 1.5 },
    },
    {
      id: "place-town",
      type: "symbol",
      ...src,
      "source-layer": "place",
      minzoom: 8,
      filter: ["==", ["get", "class"], "town"],
      layout: {
        "text-field": NAME,
        "text-font": REGULAR,
        "text-size": grow(8, 11, 14, 15),
      },
      paint: { "text-color": p.label, "text-halo-color": p.halo, "text-halo-width": 1.5 },
    },
    {
      id: "place-city",
      type: "symbol",
      ...src,
      "source-layer": "place",
      filter: ["==", ["get", "class"], "city"],
      layout: {
        "text-field": NAME,
        "text-font": BOLD,
        "text-size": grow(5, 11, 14, 18),
      },
      paint: { "text-color": p.label, "text-halo-color": p.halo, "text-halo-width": 2 },
    },
  ];
}

export function mapStyle(dark: boolean): StyleSpecification {
  return {
    version: 8,
    glyphs: GLYPHS,
    sources: { osm: { type: "vector", url: TILES, attribution: ATTRIBUTION } },
    layers: layers(dark ? DARK : LIGHT),
  };
}
