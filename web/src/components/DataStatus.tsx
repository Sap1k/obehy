import { IconCheck, IconClockExclamation, IconCloudOff } from "@tabler/icons-react";
import type { Freshness } from "../lib/freshness";
import classes from "./DataStatus.module.css";

const LOOK = {
  live: { label: "Živá data", Icon: IconCheck },
  delayed: { label: "Zpožděná data", Icon: IconClockExclamation },
  offline: { label: "Offline · uložená data", Icon: IconCloudOff },
} as const;

export function DataStatus({ freshness }: { freshness: Freshness }) {
  const { label, Icon } = LOOK[freshness];
  return (
    <span className={`${classes.root} ${classes[freshness]} ob-fade-in`} role="status">
      <Icon size={14} stroke={2} />
      {label}
    </span>
  );
}
