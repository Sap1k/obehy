import { useMediaQuery } from "@mantine/hooks";
import { Outlet, useParams } from "@tanstack/react-router";
import { useState } from "react";
import { BottomSheet, type SheetSnap } from "../../components/BottomSheet";
import { Panel } from "../../components/Panel";
import { MapContext } from "./context";
import { MapStage } from "./MapStage";
import classes from "./MapPage.module.css";

// The panel and the map load together (one chunk), so a click opens it without another
// download.
export { VehiclePanel } from "./VehiclePanel";
export { MapStage };

/** The map tab. On the desktop the map is the shell's base and this shows only the open
 * vehicle's panel; on the phone it is the map, with the vehicle in a bottom sheet. */
export function MapPage() {
  const { vehicleId } = useParams({ strict: false }) as { vehicleId?: string };
  const phone = useMediaQuery("(max-width: 48em)", false, { getInitialValueInEffect: false });
  const [snap, setSnap] = useState<SheetSnap>("half");
  const [sheetPx, setSheetPx] = useState(0);
  const open = Boolean(vehicleId);

  return (
    <MapContext.Provider value={{ phone, snap, setSnap }}>
      {phone ? (
        <div className={classes.root}>
          <MapStage bottomInset={open ? sheetPx : 0} />
          {open && (
            <BottomSheet snap={snap} onSnap={setSnap} onHeight={setSheetPx}>
              <Outlet />
            </BottomSheet>
          )}
        </div>
      ) : (
        open && (
          <Panel name="vehicle" label="Vozidlo" flush>
            <Outlet />
          </Panel>
        )
      )}
    </MapContext.Provider>
  );
}
