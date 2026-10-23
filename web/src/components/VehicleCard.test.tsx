import { MantineProvider } from "@mantine/core";
import { render, screen } from "@testing-library/react";
import type { TripVehicle } from "../api/client";
import { VehicleCard } from "./VehicleCard";

const vehicle = (traits: TripVehicle["traits"]): TripVehicle =>
  ({ vehicle_id: "pid:3:7621", traits }) as TripVehicle;

const card = (traits: TripVehicle["traits"]) =>
  render(
    <MantineProvider>
      <VehicleCard vehicle={vehicle(traits)} mode="bus" />
    </MantineProvider>,
  );

describe("VehicleCard", () => {
  it("is named by its model, its number and operator below", () => {
    card({
      model: "Iveco Crossway LE CITY 12M",
      fleet_number: "7621",
      operator: "ARRIVA",
      licence_plate: "1TD8408",
    });
    expect(screen.getByText("Iveco Crossway LE CITY 12M")).toBeInTheDocument();
    expect(screen.getByText("č. 7621 · ARRIVA")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: /SPZ 1TD 8408/ })).toHaveAttribute(
      "href",
      "https://seznam-autobusu.cz/seznam?spz=1TD%208408",
    );
  });

  it("is named by its plate without a model, which links from the title", () => {
    card({ fleet_number: "7621", licence_plate: "1TD8408" });
    const title = screen.getByRole("link", { name: /1TD 8408/ });
    expect(title).toHaveTextContent("1TD 8408");
    expect(screen.queryByText(/SPZ/)).not.toBeInTheDocument();
    expect(screen.getByText("č. 7621")).toBeInTheDocument();
  });

  it("is named by its fleet number when that is all it has", () => {
    card({ fleet_number: "7621" });
    expect(screen.getByText("7621")).toBeInTheDocument();
    expect(screen.queryByText("č. 7621")).not.toBeInTheDocument();
  });
});
