import { render } from "@testing-library/react";
import { linkify } from "./linkify";

const links = (text: string) => {
  const { container } = render(<p>{linkify(text)}</p>);
  return {
    text: container.textContent,
    hrefs: [...container.querySelectorAll("a")].map((a) => [a.textContent, a.getAttribute("href")]),
  };
};

describe("linkify", () => {
  it("links the phone line and the e-mail of a carrier's note", () => {
    const note =
      "Pro reklamace a dotazy využijte telefonní linku +420 725 100 725 nebo e-mailovou adresu info@arriva.cz.";
    const found = links(note);
    expect(found.text).toBe(note);
    expect(found.hrefs).toEqual([
      ["+420 725 100 725", "tel:+420725100725"],
      ["info@arriva.cz", "mailto:info@arriva.cz"],
    ]);
  });

  it("links web addresses to a new tab, without the sentence's punctuation", () => {
    const { container } = render(
      <p>
        {linkify(
          "Informace na www.arriva.cz, jízdní řády na https://idos.cz/vlaky/. Tarif viz idsjmk.cz.",
        )}
      </p>,
    );
    const anchors = [...container.querySelectorAll("a")];
    expect(anchors.map((a) => [a.textContent, a.getAttribute("href")])).toEqual([
      ["www.arriva.cz", "https://www.arriva.cz"],
      ["https://idos.cz/vlaky/", "https://idos.cz/vlaky/"],
      ["idsjmk.cz", "https://idsjmk.cz"],
    ]);
    expect(anchors.every((a) => a.target === "_blank" && a.querySelector("svg"))).toBe(true);
    expect(links("Pište na info@arriva.cz.").hrefs).toEqual([
      ["info@arriva.cz", "mailto:info@arriva.cz"],
    ]);
  });

  it("adds the country code to a bare number and leaves other numbers alone", () => {
    expect(links("Objednávky na 800 100 200 do 18 h.").hrefs).toEqual([
      ["800 100 200", "tel:+420800100200"],
    ]);
    expect(links("Grafikony: PD T7932, č. 1234567890, 12 345 678").hrefs).toEqual([]);
  });
});
