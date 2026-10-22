import type { Trip } from "../../api/client";
import { noteGroups } from "./notes";

const amenities = (operates: string[]) =>
  ({ operates: operates.map((cs) => ({ cs, en: cs })) }) as Trip["amenities"];

describe("noteGroups", () => {
  it("lists a train's central notes once each, by their short label", () => {
    const groups = noteGroups({
      amenities: amenities([]),
      notes: [
        { kind: "czptt_central_note", code: "22", label: "kolo", text: "přeprava spoluzavazadel" },
        { kind: "czptt_central_note", code: "40", label: "WiFi", text: "ve vlaku je wifi" },
        { kind: "czptt_central_note", code: "22", label: "kolo", text: "přeprava spoluzavazadel" },
      ],
    });
    expect(groups.short.map((n) => n.label)).toEqual(["kolo", "WiFi"]);
    expect(groups.long).toEqual([]);
  });

  it("keeps when it runs apart from the longer texts", () => {
    const groups = noteGroups({
      amenities: amenities(["jede v pracovních dnech"]),
      notes: [
        { kind: "route_information", label: null, text: "Grafikony: PD T3719" },
        { kind: "route_information", label: null, text: "Grafikony: PD T3719" },
        { kind: "calendar", label: null, text: "jede v pracovních dnech" },
      ],
    });
    expect(groups).toEqual({
      operates: ["jede v pracovních dnech"],
      short: [],
      long: ["Grafikony: PD T3719"],
    });
  });
});
