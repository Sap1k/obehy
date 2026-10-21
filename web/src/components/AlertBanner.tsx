import { Collapse, UnstyledButton } from "@mantine/core";
import { IconAlertTriangle, IconChevronDown, IconChevronRight } from "@tabler/icons-react";
import { useState } from "react";
import type { Alert } from "../api/client";
import { localised, periodText } from "../lib/alerts";
import classes from "./AlertBanner.module.css";

/** An alert in the info colours: its header, a short line under it, the text on click. */
export function AlertBanner({
  alert,
  now,
  context,
}: {
  alert: Alert;
  now: number;
  context?: string;
}) {
  const [open, setOpen] = useState(false);
  const description = localised(alert.description);
  const url = localised(alert.url);
  const meta = [context, periodText(alert, now)].filter(Boolean).join(" · ");
  const expandable = Boolean(description || url);
  const Chevron = open ? IconChevronDown : IconChevronRight;
  return (
    <div className={classes.root}>
      <UnstyledButton
        className={classes.head}
        onClick={() => setOpen((o) => !o)}
        disabled={!expandable}
        aria-expanded={expandable ? open : undefined}
      >
        <IconAlertTriangle size={20} stroke={2} className={classes.icon} />
        <span className={classes.texts}>
          <span className={classes.header}>{localised(alert.header) || "Mimořádnost"}</span>
          {meta && <span className={classes.meta}>{meta}</span>}
        </span>
        {expandable && <Chevron size={20} stroke={2} className={classes.chevron} />}
      </UnstyledButton>
      {expandable && (
        <Collapse expanded={open}>
          <div className={classes.body}>
            {description && <p>{description}</p>}
            {url && (
              <a href={url} target="_blank" rel="noreferrer">
                Více u dopravce
              </a>
            )}
          </div>
        </Collapse>
      )}
    </div>
  );
}
