import { validateStyleMin } from "@maplibre/maplibre-gl-style-spec";
import { mapStyle } from "./style";

describe("mapStyle", () => {
  it.each([false, true])("is a valid MapLibre style (dark: %s)", (dark) => {
    expect(validateStyleMin(mapStyle(dark))).toEqual([]);
  });
});
