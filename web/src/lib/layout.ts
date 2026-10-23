import { useMediaQuery } from "@mantine/hooks";

/** The phone layout (Mantine's `sm` breakpoint, as in the CSS): tabs are pages, the map is the
 * map tab's, a vehicle opens in a bottom sheet. */
export function usePhone(): boolean {
  return useMediaQuery("(max-width: 48em)", false, { getInitialValueInEffect: false });
}
