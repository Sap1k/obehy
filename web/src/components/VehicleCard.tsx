import { IconBus, IconSnowflake, IconTrain, IconWheelchair } from "@tabler/icons-react";
import type { TripVehicle } from "../api/client";
import classes from "./VehicleCard.module.css";

const RAIL_MODES = new Set([
  "rail",
  "tram",
  "subway",
  "metro",
  "funicular",
  "cable_car",
  "monorail",
]);

/** The vehicle on a trip (Figma "Vehicle card"): mode tile, number, model · operator, traits. */
export function VehicleCard({ vehicle, mode }: { vehicle: TripVehicle; mode: string }) {
  const traits = vehicle.traits ?? {};
  const Mode = RAIL_MODES.has(mode) ? IconTrain : IconBus;
  const number = traits.fleet_number ?? traits.licence_plate ?? vehicle.vehicle_id;
  const meta = [traits.model, traits.operator].filter(Boolean).join(" · ");
  const chips = [
    (traits.low_floor || traits.wheelchair_accessible) && {
      label: "Bezbariérový",
      Icon: IconWheelchair,
    },
    traits.air_conditioning && { label: "Klimatizace", Icon: IconSnowflake },
  ].filter((c): c is { label: string; Icon: typeof IconWheelchair } => Boolean(c));
  return (
    <div className={classes.root}>
      <div className={classes.head}>
        <span className={classes.tile} aria-hidden>
          <Mode size={20} stroke={2} />
        </span>
        <span className={classes.texts}>
          <span className={classes.number}>{number}</span>
          {meta && <span className={classes.meta}>{meta}</span>}
        </span>
      </div>
      {chips.length > 0 && (
        <ul className={classes.chips}>
          {chips.map(({ label, Icon }) => (
            <li key={label} className={classes.chip}>
              <Icon size={14} stroke={2} aria-hidden />
              {label}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
