import { Outlet } from "@tanstack/react-router";
import { BottomSheet } from "../../components/BottomSheet";
import { Panel } from "../../components/Panel";
import { usePhone } from "../../lib/layout";
import { useOpened } from "./selection";

// The panel and the map load together (one chunk), so a click opens it without another
// download.
export { VehiclePanel } from "./VehiclePanel";
export { MapStage } from "./MapStage";

/** The map tab: the map is the shell's; this is the open vehicle's place over it, a panel on
 * the web, a bottom sheet on the phone. */
export function MapPage() {
  const phone = usePhone();
  const opened = useOpened();
  if (opened.kind !== "vehicle") return null;
  return phone ? (
    <BottomSheet>
      <Outlet />
    </BottomSheet>
  ) : (
    <Panel label="Vozidlo" flush>
      <Outlet />
    </Panel>
  );
}
