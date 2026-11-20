import { stopLabels, tripFeatures } from "./tripLayer";

describe("stopLabels", () => {
  it("leaves out the town most of the trip runs in, starting with a capital", () => {
    expect(stopLabels(["Praha,Zličín", "Praha,Můstek"])).toEqual(["Zličín", "Můstek"]);
    expect(stopLabels(["Nový Bor,,nám.", "Nový Bor,,žel.st."])).toEqual(["Nám.", "Žel.st."]);
    // A city line reaching past the boundary: only the city's own stops lose its name.
    expect(
      stopLabels(["Praha,Černý Most", "Praha,Rajská zahrada", "Praha,Lehovec", "Zeleneč,náves"]),
    ).toEqual(["Černý Most", "Rajská zahrada", "Lehovec", "Zeleneč, náves"]);
  });

  it("shows whole names when no town has most of the stops", () => {
    expect(
      stopLabels(["Kolín,Karlovo náměstí", "Kolín,Zálabí", "Nová Ves I", "Nový Bor,,nám."]),
    ).toEqual(["Kolín, Karlovo náměstí", "Kolín, Zálabí", "Nová Ves I", "Nový Bor, nám."]);
    expect(stopLabels(["Praha,Černý Most", "Brandýs n.L.-St.Bol.,Brandýs,Zámek"])).toEqual([
      "Praha, Černý Most",
      "Brandýs n.L.-St.Bol., Brandýs, Zámek",
    ]);
  });

  it("keeps names without a town as they are", () => {
    expect(stopLabels(["Praha hlavní nádraží", "Kolín"])).toEqual([
      "Praha hlavní nádraží",
      "Kolín",
    ]);
  });
});

describe("tripFeatures", () => {
  it("is the path and each stop, or nothing", () => {
    expect(tripFeatures(null).features).toEqual([]);
    const fc = tripFeatures({
      paths: [
        [
          [14, 50],
          [14.1, 50.1],
        ],
      ],
      stops: [{ id: "s1", position: [14, 50], name: "A", passed: true, rank: 1 }],
      colour: "#ff0000",
    });
    expect(fc.features.map((f) => f.properties?.kind)).toEqual(["path", "stop"]);
    expect(fc.features[1]!.properties).toMatchObject({ id: "s1", name: "A", rank: 1 });
  });
});
