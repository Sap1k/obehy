import type { Trip } from "../../api/client";
import { noteGroups } from "./notes";
import { hasPictogram } from "../../components/Pictogram";

type Feature = Trip["features"][number];

const feature = (
  system: "jdf" | "czptt_note" | null,
  code: string | null,
  text: string,
  extra: Partial<Feature> = {},
): Feature => ({
  kinds: ["note"],
  group: "note",
  icon: "note",
  label: { cs: text, en: text },
  official: true,
  scope: "trip",
  from_sequence: null,
  to_sequence: null,
  source_code: system && code ? { system, code, name: null, text } : null,
  ...extra,
});

const day = (code: string, text: string) =>
  feature("jdf", code, text, { kinds: ["calendar_designation"], group: "calendar" });

describe("noteGroups", () => {
  it("lists central notes once each, by their text, in the KADR print order", () => {
    const groups = noteGroups({
      features: [
        feature("czptt_note", "44", "vlak zařazený v integrovaném dopravním systému"),
        feature("czptt_note", "40", "ve vlaku je řazen vůz s bezdrátovým připojením k internetu"),
        feature("czptt_note", "12", "ve vlaku řazeny k sezení pouze vozy 2. vozové třídy"),
        feature("czptt_note", "40", "ve vlaku je řazen vůz s bezdrátovým připojením k internetu", {
          scope: "call_range",
          from_sequence: 3,
          to_sequence: 5,
        }),
      ],
    });
    expect(groups.legend.map((n) => n.key)).toEqual([
      "czptt_note:12",
      "czptt_note:40",
      "czptt_note:44",
    ]);
    expect(JSON.stringify(groups)).not.toContain("dvojka");
  });

  it("signs a central note stated for some calls by its note code, with its text", () => {
    const groups = noteGroups({
      features: [
        feature(null, null, "ve vlaku je řazen vůz s přípojkou 230 V", {
          scope: "call_range",
          note: {
            kind: "czptt_central_note",
            code: "30",
            label: "zásuvka",
            text: "ve vlaku je řazen vůz s přípojkou 230 V",
          },
        }),
      ],
    });
    expect(groups.legend).toEqual([
      { key: "czptt_note:30", text: "ve vlaku je řazen vůz s přípojkou 230 V" },
    ]);
  });

  it("shows the plain signs alone, states the others and keeps the details for later", () => {
    const groups = noteGroups({
      features: [
        feature("czptt_note", "40", "ve vlaku je řazen vůz s bezdrátovým připojením k internetu"),
        feature("czptt_note", "33", "vůz nebo oddíly vyhrazené pro cestující s dětmi do 10 let"),
        feature("czptt_note", "32", "náhradní autobusová doprava"),
      ],
    });
    expect(groups.plain.map((n) => n.key)).toEqual(["czptt_note:40"]);
    expect(groups.stated.map((n) => n.key)).toEqual(["czptt_note:32"]);
    expect(groups.legend.map((n) => n.key)).toEqual(["czptt_note:33", "czptt_note:40"]);
    expect(groups.detail.map((n) => n.key)).toEqual(["czptt_note:33"]);
  });

  it("badges a wheelchair booking and keeps its detail in the legend", () => {
    const groups = noteGroups({
      features: [
        feature("czptt_note", "34", "vůz vhodný pro přepravu cestujících na vozíku"),
        feature("czptt_note", "40", "ve vlaku je řazen vůz s bezdrátovým připojením k internetu"),
      ],
    });
    expect(groups.stated).toEqual([]);
    expect(groups.plain.map((n) => n.key)).toEqual(["czptt_note:34", "czptt_note:40"]);
    expect(groups.legend.map((n) => n.key)).toEqual(["czptt_note:34", "czptt_note:40"]);
  });

  it("reads the days of operation in week order", () => {
    const groups = noteGroups({
      features: [
        day("7", "jede v neděli"),
        day("+", "jede v neděli a ve státem uznané svátky"),
        day("6", "jede v sobotu"),
        day("X", "jede v pracovních dnech"),
      ],
    });
    expect(groups.operates.map((n) => n.key)).toEqual(["jdf:X", "jdf:6", "jdf:7", "jdf:+"]);
  });

  it("leaves a call's stopping codes to the timetable", () => {
    const groups = noteGroups({
      features: [
        feature("jdf", "x", "spoj zastavuje jen na znamení nebo požádání", {
          kinds: ["request_stop"],
          scope: "call",
          from_sequence: 4,
        }),
        feature("jdf", "O", "spoj přepravuje jízdní kola", { kinds: ["bicycle_transport"] }),
      ],
    });
    expect(groups.legend.map((n) => n.key)).toEqual(["jdf:O"]);
  });

  it("keeps a code without a sign, last", () => {
    const groups = noteGroups({
      features: [
        feature("czptt_note", "99", "nová poznámka"),
        feature("czptt_note", "22", "přeprava spoluzavazadel (do vyčerpání kapacity)"),
      ],
    });
    expect(groups.legend.map((n) => n.key)).toEqual(["czptt_note:22", "czptt_note:99"]);
    expect(hasPictogram("czptt_note:99")).toBe(false);
  });

  it("keeps the longer texts apart from the days of operation", () => {
    const route = { kind: "route_information", label: null, text: "Grafikony: PD T3719" };
    const calendar = { kind: "calendar", label: null, text: "jede v pracovních dnech" };
    const groups = noteGroups({
      features: [
        day("X", "jede v pracovních dnech"),
        feature(null, null, "Grafikony: PD T3719", { note: route }),
        feature(null, null, "Grafikony: PD T3719", { note: route }),
        feature(null, null, "jede v pracovních dnech", { note: calendar }),
      ],
    });
    expect(groups.operates.map((n) => n.text)).toEqual(["jede v pracovních dnech"]);
    expect(groups.legend).toEqual([]);
    expect(groups.long).toEqual(["Grafikony: PD T3719"]);
  });
});
