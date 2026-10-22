import { createContext, useContext } from "react";
import type { SheetSnap } from "../../components/BottomSheet";

/** What the map tab shares with the vehicle panel: whether it is in the phone's sheet, and the
 * sheet's snap. The open vehicle and following are the map store's (`store.ts`). */
export interface MapState {
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
