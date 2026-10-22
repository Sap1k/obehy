import { MantineProvider } from "@mantine/core";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import type { Vehicle } from "../../api/client";
import { Search } from "./Search";

const navigate = vi.fn();
vi.mock("@tanstack/react-router", () => ({ useNavigate: () => navigate }));

const tram = (id: string, headsign: string, fleet: string): Vehicle => ({
  vehicle_id: id,
  feed: "jdf",
  source: "pid",
  state: "running",
  last_seen: "2026-10-22T20:00:00Z",
  latitude: 50.08,
  longitude: 14.42,
  bearing: null,
  trip_id: "jdf:trip:1",
  service_date: "2026-10-22",
  line: {
    route_id: "r9",
    short_name: "9",
    long_name: null,
    mode: "tram",
    color: "7A0603",
    text_color: "FFFFFF",
  },
  trip_short_name: null,
  headsign,
  delay_s: 0,
  traits: { fleet_number: fleet },
  sources: ["pid"],
});

const get = vi.fn();
vi.mock("../../api/client", async (original) => ({
  ...(await original<typeof import("../../api/client")>()),
  api: { GET: (...args: unknown[]) => get(...args) },
}));

function renderSearch() {
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  render(
    <MantineProvider env="test">
      <QueryClientProvider client={client}>
        <Search />
      </QueryClientProvider>
    </MantineProvider>,
  );
  return screen.getByRole("textbox", { name: "Hledat zastávku nebo vozidlo" });
}

const answer = (data: unknown) => ({ data, response: new Response(null, { status: 200 }) });

describe("Search", () => {
  beforeEach(() => {
    get.mockReset();
    navigate.mockReset();
  });

  it("finds vehicles by a line of one character, and opens the one picked", async () => {
    get.mockImplementation((path: string) =>
      Promise.resolve(
        answer(
          path === "/api/v1/vehicles"
            ? { vehicles: [tram("pid:0:8151", "Sídliště Spořilov", "8151")] }
            : { stops: [] },
        ),
      ),
    );
    const input = renderSearch();
    fireEvent.change(input, { target: { value: "9" } });
    const option = await screen.findByText("Sídliště Spořilov");
    expect(screen.getByText("Vozidla")).toBeInTheDocument();
    expect(screen.getByText("č. 8151")).toBeInTheDocument();
    // One character is no stop search (the API's shortest is two).
    expect(get).toHaveBeenCalledWith("/api/v1/vehicles", expect.anything());
    expect(get).not.toHaveBeenCalledWith("/api/v1/stops", expect.anything());
    fireEvent.click(option);
    expect(navigate).toHaveBeenCalledWith({
      to: "/vozidlo/$vehicleId",
      params: { vehicleId: "pid:0:8151" },
    });
  });

  it("lists stops first for a name, vehicles first for a number", async () => {
    get.mockImplementation((path: string) =>
      Promise.resolve(
        answer(
          path === "/api/v1/vehicles"
            ? { vehicles: [tram("pid:0:1", "Bílá Hora", "1")] }
            : {
                stops: [{ location_id: "s1", name: "Bílá Hora", municipality: "Praha" }],
              },
        ),
      ),
    );
    const input = renderSearch();
    fireEvent.change(input, { target: { value: "bílá hora" } });
    await screen.findByText("Zastávky");
    const labels = () =>
      [...document.querySelectorAll(".mantine-Combobox-groupLabel")].map((g) => g.textContent);
    await waitFor(() => expect(labels()).toEqual(["Zastávky", "Vozidla"]));
    fireEvent.change(input, { target: { value: "22" } });
    await waitFor(() => expect(labels()).toEqual(["Vozidla", "Zastávky"]));
  });

  it("says the search failed when the only search that runs fails", async () => {
    get.mockResolvedValue({
      error: { title: "Bad" },
      response: new Response(null, { status: 422 }),
    });
    const input = renderSearch();
    fireEvent.change(input, { target: { value: "9" } });
    expect(await screen.findByText("Hledání se nepodařilo")).toBeInTheDocument();
  });
});
