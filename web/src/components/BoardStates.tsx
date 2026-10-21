import { Button, Skeleton } from "@mantine/core";
import classes from "./BoardStates.module.css";

/** The first answer only; later refreshes keep the rows (Figma 18, "Načítání"). */
export function BoardSkeleton({ rows = 4 }: { rows?: number }) {
  return (
    <div className={classes.card} aria-busy="true" aria-label="Načítání">
      {Array.from({ length: rows }, (_, i) => (
        <div key={i} className={classes.skeletonRow}>
          <Skeleton height={24} width={48} radius="sm" />
          <Skeleton height={24} width={40} radius="sm" />
          <Skeleton height={14} width="45%" radius="sm" />
        </div>
      ))}
    </div>
  );
}

export function BoardMessage({
  title,
  text,
  action,
}: {
  title: string;
  text: string;
  action?: { label: string; onClick: () => void; primary?: boolean };
}) {
  return (
    <div className={`${classes.card} ${classes.message}`}>
      <strong className={classes.title}>{title}</strong>
      <span className={classes.text}>{text}</span>
      {action &&
        (action.primary ? (
          <Button onClick={action.onClick} mt="sm">
            {action.label}
          </Button>
        ) : (
          <button type="button" className={classes.link} onClick={action.onClick}>
            {action.label}
          </button>
        ))}
    </div>
  );
}

/** "Zítra · čtvrtek 22. října" between the rows of two days. */
export function DayDivider({ label }: { label: string }) {
  return (
    <div className={classes.divider} role="separator">
      {label}
    </div>
  );
}
