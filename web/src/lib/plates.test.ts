import { plateUrl } from "./plates";

describe("plates", () => {
  it("links to the vehicle's search on seznam-autobusu.cz", () => {
    expect(plateUrl("2UC 6580")).toBe("https://seznam-autobusu.cz/seznam?spz=2UC%206580");
  });
});
