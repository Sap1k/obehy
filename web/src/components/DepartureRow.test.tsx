import { MantineProvider } from "@mantine/core";
import { render, screen, within } from "@testing-library/react";
import type { Departure } from "../api/client";
import board from "../test/fixtures/board-decin.json";
import { DepartureRow } from "./DepartureRow";
import { lineLabel } from "./LineBadge";

// Three rows of Děčín hlavní nádraží from obehy.cz (22 October 2026, 14:30), as the API sends them.
const rows = board as Departure[];

function renderRow(departure: Departure, offline = false) {
  const { container } = render(
    <MantineProvider env="test">
      <div data-testid="row">
        <DepartureRow departure={departure} offline={offline} />
      </div>
    </MantineProvider>,
  );
  return within(container.querySelector("[data-testid=row]") as HTMLElement);
}

const at = (iso: string, minutes: number) =>
  new Date(Date.parse(iso) + minutes * 60_000).toISOString();

describe("DepartureRow", () => {
  it("shows a timetable row as the API sends it", () => {
    const first = rows[0]!;
    const row = renderRow(first);
    expect(row.getByText("14:30")).toBeInTheDocument();
    expect(row.getByText("Dle JŘ")).toBeInTheDocument();
    expect(row.getByText("U1")).toBeInTheDocument();
    expect(row.getByText(first.headsign!)).toBeInTheDocument();
    expect(row.getByText(first.trip_short_name!)).toBeInTheDocument();
    expect(row.getByTitle(`Nástupiště ${first.platform}`)).toBeInTheDocument();
  });

  it("shows only the category of a train without a line", () => {
    const lineless = rows[1]!;
    expect(lineless.line.short_name).toBe(lineless.trip_short_name);
    renderRow(lineless);
    expect(screen.getByText("Os")).toBeInTheDocument();
    expect(lineLabel({ short_name: "U8", long_name: null }, "Os 6658")).toBe("U8");
  });

  it("colours a live delay and words it", () => {
    const first = rows[0]!;
    const row = renderRow({
      ...first,
      status: "predicted",
      estimated: at(first.scheduled, 4),
      delay_s: 240,
    });
    expect(row.getByText("14:34")).toBeInTheDocument();
    expect(row.getByText("+4 min")).toBeInTheDocument();
  });

  it("reads neutral and approximate offline", () => {
    const first = rows[0]!;
    const row = renderRow(
      { ...first, status: "predicted", estimated: at(first.scheduled, 4), delay_s: 240 },
      true,
    );
    expect(row.getByText("≈ +4 min")).toBeInTheDocument();
  });

  it("strikes a cancelled departure", () => {
    const row = renderRow({ ...rows[2]!, status: "cancelled" });
    expect(row.getByText("Zrušeno")).toBeInTheDocument();
  });

  it("marks step-free vehicles and alerts", () => {
    const first = rows[0]!;
    const row = renderRow({
      ...first,
      alert_ids: ["pid:1"],
      vehicle: {
        vehicle_id: "pid:1",
        state: "running",
        last_seen: first.scheduled,
        latitude: null,
        longitude: null,
        bearing: null,
        traits: { low_floor: true, air_conditioning: true },
        sources: [],
      },
    });
    expect(row.getByLabelText("Bezbariérový")).toBeInTheDocument();
    expect(row.getByLabelText("Klimatizace")).toBeInTheDocument();
    expect(row.getByText("Mimořádnost")).toBeInTheDocument();
  });
});
