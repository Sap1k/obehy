import { Combobox, Loader, TextInput, useCombobox } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { IconSearch } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { api, unwrap, type Stop, type Vehicle } from "../../api/client";
import { LineBadge, lineLabel } from "../../components/LineBadge";
import { distanceText, metresBetween, roughly, useKnownPosition } from "../../lib/position";
import classes from "./Search.module.css";

/** The API's shortest stop name search; a vehicle is found by one character (line 2). */
const STOP_MIN_CHARS = 2;
const STOPS = 6;
const VEHICLES = 8;

/** A line or a number reads as a vehicle search first ("22", "S49", "6819"); a name, as a
 * stop search first. */
const LOOKS_LIKE_A_VEHICLE = /^\s*[\p{L}]{0,3}\s*\d/u;

/** Search (Figma "Search"): stops by name and municipality, and vehicles by line, train number,
 * headsign, fleet number or plate, the nearest first when the site may know where one is.
 * Debounced; Enter opens the first result. */
export function Search({
  autoFocus = false,
  onPicked,
}: {
  autoFocus?: boolean;
  onPicked?: () => void;
}) {
  const navigate = useNavigate();
  const combobox = useCombobox({ onDropdownClose: () => combobox.resetSelectedOption() });
  const [text, setText] = useState("");
  const [q] = useDebouncedValue(text.trim(), 200);
  const position = useKnownPosition();
  const near = position ? roughly(position) : undefined;

  const stopsOn = q.replace(/[^\p{L}\p{N}]/gu, "").length >= STOP_MIN_CHARS;
  const vehiclesOn = /[\p{L}\p{N}]/u.test(q);
  const stopResults = useQuery({
    queryKey: ["stops", q],
    queryFn: async ({ signal }) =>
      unwrap(await api.GET("/api/v1/stops", { params: { query: { q, limit: STOPS } }, signal })),
    enabled: stopsOn,
    staleTime: 5 * 60_000,
  });
  const vehicleResults = useQuery({
    queryKey: ["vehicle-search", q, near],
    queryFn: async ({ signal }) =>
      unwrap(
        await api.GET("/api/v1/vehicles", {
          params: { query: { q, near, limit: VEHICLES } },
          signal,
        }),
      ),
    enabled: vehiclesOn,
    staleTime: 10_000,
  });
  const stops = useMemo(
    () => (stopsOn ? (stopResults.data?.stops ?? []) : []),
    [stopsOn, stopResults.data],
  );
  const vehicles = useMemo(
    () => (vehiclesOn ? (vehicleResults.data?.vehicles ?? []) : []),
    [vehiclesOn, vehicleResults.data],
  );
  const vehiclesFirst = LOOKS_LIKE_A_VEHICLE.test(q);
  const any = stops.length + vehicles.length > 0;
  const settled =
    (!stopsOn || !stopResults.isFetching) && (!vehiclesOn || !vehicleResults.isFetching);
  // Failed: every search that runs failed.
  const failed =
    (!stopsOn || stopResults.isError) && (!vehiclesOn || vehicleResults.isError) && vehiclesOn;

  useEffect(() => {
    if (any) combobox.selectFirstOption();
  }, [stops, vehicles, any, combobox]);

  const open = (value: string) => {
    combobox.closeDropdown();
    setText("");
    onPicked?.();
    const [kind, ...rest] = value.split(":");
    const id = rest.join(":");
    if (kind === "vehicle") void navigate({ to: "/vozidlo/$vehicleId", params: { vehicleId: id } });
    else void navigate({ to: "/odjezdy/$stopId", params: { stopId: id } });
  };

  const stopGroup = stops.length > 0 && (
    <Combobox.Group label="Zastávky" key="stops">
      {stops.map((s) => (
        <StopOption key={s.location_id} stop={s} />
      ))}
    </Combobox.Group>
  );
  const vehicleGroup = vehicles.length > 0 && (
    <Combobox.Group label="Vozidla" key="vehicles">
      {vehicles.map((v) => (
        <VehicleOption key={v.vehicle_id} vehicle={v} position={position} />
      ))}
    </Combobox.Group>
  );

  return (
    <Combobox store={combobox} onOptionSubmit={open} withinPortal={false}>
      <Combobox.Target>
        <TextInput
          className={classes.input}
          size="md"
          autoFocus={autoFocus}
          value={text}
          placeholder="Zastávka, linka, vlak, vozidlo"
          aria-label="Hledat zastávku nebo vozidlo"
          leftSection={<IconSearch size={20} stroke={2} />}
          rightSection={!settled ? <Loader size={16} /> : null}
          onChange={(e) => {
            setText(e.currentTarget.value);
            combobox.openDropdown();
          }}
          onFocus={() => combobox.openDropdown()}
          onBlur={() => combobox.closeDropdown()}
        />
      </Combobox.Target>
      <Combobox.Dropdown hidden={!vehiclesOn}>
        <Combobox.Options className={classes.options}>
          {vehiclesFirst ? [vehicleGroup, stopGroup] : [stopGroup, vehicleGroup]}
          {settled && !any && !failed && <Combobox.Empty>Nic jsme nenašli</Combobox.Empty>}
          {failed && <Combobox.Empty>Hledání se nepodařilo</Combobox.Empty>}
        </Combobox.Options>
      </Combobox.Dropdown>
    </Combobox>
  );
}

function StopOption({ stop }: { stop: Stop }) {
  return (
    <Combobox.Option value={`stop:${stop.location_id}`} className={classes.option}>
      <span className={classes.texts}>
        <span className={classes.name}>{stop.name}</span>
        {stop.municipality && stop.municipality !== stop.name && (
          <span className={classes.place}>{stop.municipality}</span>
        )}
      </span>
    </Combobox.Option>
  );
}

function VehicleOption({
  vehicle,
  position,
}: {
  vehicle: Vehicle;
  position: ReturnType<typeof useKnownPosition>;
}) {
  const traits = vehicle.traits ?? {};
  const number = traits.fleet_number ?? traits.licence_plate;
  // The distance from where one really is (the request carried it only roughly).
  const distance =
    position && vehicle.latitude !== null && vehicle.longitude !== null
      ? distanceText(metresBetween(position, vehicle.latitude, vehicle.longitude))
      : null;
  const meta = [vehicle.trip_short_name, number && `č. ${number}`, distance]
    .filter(Boolean)
    .join(" · ");
  return (
    <Combobox.Option value={`vehicle:${vehicle.vehicle_id}`} className={classes.option}>
      {vehicle.line && <LineBadge line={vehicle.line} tripShortName={vehicle.trip_short_name} />}
      <span className={classes.texts}>
        <span className={classes.name}>
          {vehicle.headsign ?? (vehicle.line ? lineLabel(vehicle.line) : "Vozidlo bez spoje")}
        </span>
        {meta && <span className={classes.place}>{meta}</span>}
      </span>
    </Combobox.Option>
  );
}
