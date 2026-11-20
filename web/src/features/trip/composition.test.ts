import { describe, expect, it } from "vitest";
import { classLabel, featureText, unitName, type CompositionUnit } from "./composition";

const call = (sequence: number, name: string) => ({
  sequence,
  location_id: `czptt:location:${name}`,
  name,
});
const CHEB = call(1, "Cheb");
const PRAHA = call(7, "Praha");

const unit = (sequence: number, over: Partial<CompositionUnit> = {}): CompositionUnit => ({
  source: "cd",
  label: { cs: "ČD", en: "ČD" },
  sequence,
  kind: "coach",
  role: "train",
  vehicle_id: null,
  vehicle_type: "Bmpz 895",
  coach_number: String(360 + sequence),
  classes: [2],
  features: [],
  destination: null,
  details: {},
  from_call: CHEB,
  to_call: PRAHA,
  open: true,
  observed_at: "2026-10-20T09:00:00+02:00",
  ...over,
});

describe("units", () => {
  it("names units and their classes", () => {
    expect(classLabel(unit(1, { classes: [2, 1] }))).toBe("1/2");
    expect(unitName(unit(1, { classes: [1] }))).toBe("Vůz 361, 1. třída");
    expect(unitName(unit(1, { kind: "locomotive", classes: [], coach_number: null }))).toBe(
      "Lokomotiva",
    );
  });

  it("words a feature by our label, else the source's text", () => {
    const wifi = { feature: "wifi", label: { cs: "Wi-Fi", en: "Wi-Fi" }, icon: "wifi", text: null };
    const raw = { feature: "raw:cd:kolo", label: null, icon: null, text: "Kolo" };
    expect(featureText(wifi)).toBe("Wi-Fi");
    expect(featureText(raw)).toBe("Kolo");
    expect(featureText({ ...raw, text: null })).toBe("kolo");
  });
});
