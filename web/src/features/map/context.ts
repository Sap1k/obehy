import { createContext, useContext } from "react";
import type { Vehicle } from "../../api/client";
import type { SheetSnap } from "../../components/BottomSheet";

/** What the map shares with the panel of the vehicle open on it. */
export interface MapState {
  /** The open vehicle: its own answer, else the row it was clicked on; null without a position. */
  vehicle: Vehicle | null;
  following: boolean;
  setFollowing: (on: boolean) => void;
  /** The phone shows the panel in a bottom sheet. */
  phone: boolean;
  snap: SheetSnap;
  setSnap: (snap: SheetSnap) => void;
}

export const MapContext = createContext<MapState | null>(null);

export function useMapState(): MapState {
  const state = useContext(MapContext);
  if (!state) throw new Error("useMapState outside the map");
  return state;
}
