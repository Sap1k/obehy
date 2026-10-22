import type { Layer, PickingInfo } from "@deck.gl/core";
import { IconLayer, PathLayer, ScatterplotLayer, type IconLayerProps } from "@deck.gl/layers";
import {
  ARROW_ICON_PX,
  ARROW_KEY,
  PUCK_ICON_PX,
  PUCK_PX,
  hexColour,
  rgb,
  type Atlas,
} from "./badges";
import type { Positioned } from "./queries";

// The deck.gl layers drawn into the map's WebGL canvas: the selected trip's path and stops,
// then the vehicles: dots when zoomed out, pucks with bearing pointers when zoomed in, and the
// selected vehicle always as a puck with a halo.

/** From this zoom vehicles are pucks with their line's name. */
export const PUCK_ZOOM = 11;

const INK = "#153438";

export interface TripOverlay {
  /** The path's lines, [lon, lat] each. */
  paths: [number, number][][];
  stops: { position: [number, number]; passed: boolean }[];
  colour: string;
}

export interface LayerInput {
  vehicles: Positioned[];
  /** Each vehicle's puck in the atlas, in the vehicles' order. */
  keys: string[];
  atlas: Atlas;
  /** The drawn positions, [lon, lat, …] in the vehicles' order. */
  positions: Float64Array;
  /** Changes whenever `positions` does. */
  frame: number;
  zoom: number;
  selectedId: string | undefined;
  trip: TripOverlay | null;
  /** The halo's colour (the action token of the scheme). */
  accent: string;
  onPick: (vehicle: Positioned) => void;
}

const colourOf = (v: Positioned) => rgb(hexColour(v.line?.color, INK));

export function vehicleLayers(input: LayerInput): Layer[] {
  const { vehicles, keys, atlas, positions, frame, zoom, selectedId, trip, accent, onPick } = input;
  const at = (_: unknown, { index }: { index: number }): [number, number] => [
    positions[index * 2]!,
    positions[index * 2 + 1]!,
  ];
  const pick = (info: PickingInfo<Positioned>) => {
    if (info.object) onPick(info.object);
    return true;
  };
  // deck.gl makes a texture of any image source, a canvas too; its type names only URLs and
  // textures.
  const iconAtlas = atlas.canvas as unknown as IconLayerProps["iconAtlas"];
  const iconMapping = atlas.mapping;
  const pucks = zoom >= PUCK_ZOOM;
  const selectedIndex = selectedId ? vehicles.findIndex((v) => v.vehicle_id === selectedId) : -1;
  const selected = selectedIndex >= 0 ? [selectedIndex] : [];
  const layers: Layer[] = [];

  if (trip) {
    const colour = rgb(trip.colour);
    layers.push(
      new PathLayer<[number, number][]>({
        id: "trip-path",
        data: trip.paths,
        getPath: (p) => p,
        getColor: [...colour, 220],
        getWidth: 4,
        widthUnits: "pixels",
        capRounded: true,
        jointRounded: true,
      }),
      new ScatterplotLayer<TripOverlay["stops"][number]>({
        id: "trip-stops",
        data: trip.stops,
        getPosition: (s) => s.position,
        getRadius: 4,
        radiusUnits: "pixels",
        getFillColor: [255, 255, 255],
        getLineColor: (s) => (s.passed ? [150, 163, 157] : [...colour, 255]),
        lineWidthUnits: "pixels",
        getLineWidth: 2,
        stroked: true,
      }),
    );
  }

  if (!pucks) {
    layers.push(
      new ScatterplotLayer<Positioned>({
        id: "vehicle-dots",
        data: vehicles,
        getPosition: at,
        getRadius: zoom >= 9 ? 4.5 : 3.5,
        radiusUnits: "pixels",
        getFillColor: colourOf,
        getLineColor: [255, 255, 255],
        getLineWidth: 1,
        lineWidthUnits: "pixels",
        stroked: true,
        pickable: true,
        onClick: pick,
        updateTriggers: { getPosition: frame, getRadius: zoom >= 9 },
      }),
    );
  } else {
    layers.push(
      new IconLayer<Positioned>({
        id: "vehicle-arrows",
        data: vehicles,
        iconAtlas,
        iconMapping,
        getPosition: at,
        getIcon: () => ARROW_KEY,
        getSize: ARROW_ICON_PX,
        sizeUnits: "pixels",
        getAngle: (v) => (v.bearing === null ? 0 : -v.bearing),
        getColor: (v) => (v.bearing === null ? [0, 0, 0, 0] : [...colourOf(v), 255]),
        updateTriggers: { getPosition: frame },
      }),
      new IconLayer<Positioned>({
        id: "vehicle-pucks",
        data: vehicles,
        iconAtlas,
        iconMapping,
        getPosition: at,
        getIcon: (_, { index }) => keys[index]!,
        getSize: PUCK_ICON_PX,
        sizeUnits: "pixels",
        pickable: true,
        onClick: pick,
        updateTriggers: { getPosition: frame },
      }),
    );
  }

  if (selected.length > 0) {
    const atSelected = (_: unknown, { index }: { index: number }) =>
      at(null, { index: selected[index]! });
    const [r, g, b] = rgb(accent);
    layers.push(
      new ScatterplotLayer<number>({
        id: "selected-halo",
        data: selected,
        getPosition: atSelected,
        getRadius: PUCK_PX / 2 + 8,
        radiusUnits: "pixels",
        getFillColor: [r, g, b, 70],
        getLineColor: [r, g, b, 255],
        getLineWidth: 2,
        lineWidthUnits: "pixels",
        stroked: true,
        updateTriggers: { getPosition: frame, getFillColor: accent, getLineColor: accent },
      }),
      new IconLayer<number>({
        id: "selected-puck",
        data: selected,
        iconAtlas,
        iconMapping,
        getPosition: atSelected,
        getIcon: (i) => keys[i]!,
        getSize: PUCK_ICON_PX,
        sizeUnits: "pixels",
        updateTriggers: { getPosition: frame, getIcon: vehicles },
      }),
    );
  }
  return layers;
}
