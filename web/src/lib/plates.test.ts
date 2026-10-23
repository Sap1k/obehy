import { plateText, plateUrl } from "./plates";

describe("plates", () => {
  it("writes Czech plates as they are written", () => {
    expect(plateText("1TD8408")).toBe("1TD 8408");
    expect(plateText("5L05324")).toBe("5L0 5324");
    expect(plateText("EL928FK")).toBe("EL 928FK");
    expect(plateText("BM123AB")).toBe("BM123AB");
  });

  it("links to the vehicle's search on seznam-autobusu.cz", () => {
    expect(plateUrl("2UC6580")).toBe("https://seznam-autobusu.cz/seznam?spz=2UC%206580");
  });
});
