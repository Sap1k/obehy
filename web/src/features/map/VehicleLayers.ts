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

/** The selected vehicle's puck is this much larger. */
const SELECTED_SCALE = 1.15;

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
  /** A board's stop place, [lon, lat]. */
  stop: [number, number] | null;
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

  if (stop) {
    layers.push(
      new ScatterplotLayer<[number, number]>({
        id: "focus-stop",
        data: [stop],
        getPosition: (s) => s,
        getRadius: 7,
        radiusUnits: "pixels",
        getFillColor: [255, 255, 255, 255],
        getLineColor: [...rgb(accent), 255],
        getLineWidth: 3,
        lineWidthUnits: "pixels",
        stroked: true,
        updateTriggers: { getLineColor: accent },
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
    // The selected vehicle on top of the rest, a little larger: a soft shadow, its pointer (the
    // tip showing past the ring), a crisp ring (a white gap, then the accent) and its puck.
    const atSelected = (_: unknown, { index }: { index: number }) =>
      at(null, { index: selected[index]! });
    const vehicleOf = (i: number) => vehicles[i]!;
    const puckRadius = (PUCK_PX / 2) * SELECTED_SCALE;
    layers.push(
      new ScatterplotLayer<number>({
        id: "selected-shadow",
        data: selected,
        getPosition: atSelected,
        getRadius: puckRadius + 9,
        radiusUnits: "pixels",
        getFillColor: [0, 0, 0, 45],
        updateTriggers: { getPosition: frame },
      }),
      new IconLayer<number>({
        id: "selected-arrow",
        data: selected,
        iconAtlas,
        iconMapping,
        getPosition: atSelected,
        getIcon: () => ARROW_KEY,
        getSize: 56, // the tip reaches past the ring
        sizeUnits: "pixels",
        getAngle: (i) => -(vehicleOf(i).bearing ?? 0),
        getColor: (i) => (vehicleOf(i).bearing === null ? [0, 0, 0, 0] : [...rgb(accent), 255]),
        updateTriggers: { getPosition: frame, getAngle: vehicles, getColor: [vehicles, accent] },
      }),
      new ScatterplotLayer<number>({
        id: "selected-ring",
        data: selected,
        getPosition: atSelected,
        getRadius: puckRadius + 4.5,
        radiusUnits: "pixels",
        getFillColor: [255, 255, 255, 255],
        getLineColor: [...rgb(accent), 255],
        getLineWidth: 3,
        lineWidthUnits: "pixels",
        stroked: true,
        updateTriggers: { getPosition: frame, getLineColor: accent },
      }),
      new IconLayer<number>({
        id: "selected-puck",
        data: selected,
        iconAtlas,
        iconMapping,
        getPosition: atSelected,
        getIcon: (i) => keys[i]!,
        getSize: PUCK_ICON_PX * SELECTED_SCALE,
        sizeUnits: "pixels",
        updateTriggers: { getPosition: frame, getIcon: vehicles },
      }),
    );
  }
  return layers;
}
