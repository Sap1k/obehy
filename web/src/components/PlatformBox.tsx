import classes from "./PlatformBox.module.css";

/** The platform, track or post code in its outlined box; nothing when unknown. */
export function PlatformBox({ code, small = false }: { code: string | null; small?: boolean }) {
  if (!code) return null;
  return (
    <span className={`${classes.root} ${small ? classes.sm : ""}`} title={`Nástupiště ${code}`}>
      {code}
    </span>
  );
}
