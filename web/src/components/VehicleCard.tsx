import {
  IconBus,
  IconExternalLink,
  IconSnowflake,
  IconTrain,
  IconWheelchair,
} from "@tabler/icons-react";
import type { TripVehicle } from "../api/client";
import { plateText, plateUrl } from "../lib/plates";
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

/** The vehicle on a trip (Figma "Vehicle card"): mode tile, what it is, then how it is known,
 * and its traits. Named by what a rider recognises: its model, else its plate, else its fleet
 * number; the fleet number (or our ID) and the operator come below, smaller. A plate links to
 * the vehicle on seznam-autobusu.cz. */
export function VehicleCard({ vehicle, mode }: { vehicle: TripVehicle; mode: string }) {
  const traits = vehicle.traits ?? {};
  const Mode = RAIL_MODES.has(mode) ? IconTrain : IconBus;
  const plate = traits.licence_plate ?? null;
  const fleet = traits.fleet_number ?? null;
  // The model with its maker ("Iveco Crossway LE LINE 13M"); the API gives them apart.
  const model = traits.model ? [traits.manufacturer, traits.model].filter(Boolean).join(" ") : null;
  const title = model ?? (plate ? plateText(plate) : (fleet ?? vehicle.vehicle_id));
  // The identifier, unless it is the title already.
  const identifier = fleet ? (title === fleet ? null : `č. ${fleet}`) : null;
  const meta = [identifier, traits.operator].filter(Boolean).join(" · ");
  // A plate in the title links from there; else on its own line.
  const titleIsPlate = !model && plate !== null;
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
          {titleIsPlate ? (
            <a
              className={`${classes.number} ${classes.titleLink}`}
              href={plateUrl(plate)}
              target="_blank"
              rel="noreferrer"
            >
              {title}
              <IconExternalLink size={16} stroke={2} aria-label="na seznam-autobusu.cz" />
            </a>
          ) : (
            <span className={classes.number}>{title}</span>
          )}
          {meta && <span className={classes.meta}>{meta}</span>}
          {plate && !titleIsPlate && (
            <a className={classes.plate} href={plateUrl(plate)} target="_blank" rel="noreferrer">
              SPZ {plateText(plate)}
              <IconExternalLink size={12} stroke={2} aria-label="na seznam-autobusu.cz" />
            </a>
          )}
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
