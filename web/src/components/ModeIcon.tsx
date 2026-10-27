import type { ReactNode } from "react";
import classes from "./ModeIcon.module.css";

// What calls at a stop: a sign per mode (the API's `route_mode`), drawn as transit pictograms
// are: each vehicle from the side, front to the right, a solid shape with its windows cut out
// (so it holds together at 16px and shows the background through on any highlight), lines only
// for what sticks out (the tram's pantograph, the trolleybus's poles, the rails). The metro is
// the Prague metro's arrow with its M (Jiří Rathouský). The colour is the text's.

const SIZE = 16;

function Svg({ children }: { children: ReactNode }) {
  return (
    <svg viewBox="0 0 24 24" width={SIZE} height={SIZE} aria-hidden>
      {children}
    </svg>
  );
}

/** A body with its windows: one path, the windows its holes. */
const Solid = ({ d }: { d: string }) => <path d={d} fill="currentColor" fillRule="evenodd" />;
const Line = ({ d, width = 1.6 }: { d: string; width?: number }) => (
  <path
    d={d}
    fill="none"
    stroke="currentColor"
    strokeWidth={width}
    strokeLinecap="round"
    strokeLinejoin="round"
  />
);
const Wheel = ({ x, y, r }: { x: number; y: number; r: number }) => (
  <circle cx={x} cy={y} r={r} fill="currentColor" />
);
const RAIL = "M1.5 20.5h21";

/** A train, its nose to the right, on its rail. */
const train = (
  <Svg>
    <Solid d="M2 16V8a2 2 0 0 1 2-2h11c3.5 0 6 3 7 7v3zM4 8.5h9v3H4zM15 8.4c1.8.4 3.1 1.5 3.9 3.1H15z" />
    <Wheel x={6.5} y={17.8} r={1.4} />
    <Wheel x={17.5} y={17.8} r={1.4} />
    <Line d={RAIL} />
  </Svg>
);
/** A low-floor tram: a cab at either end, its single-arm pantograph up, on its rail. */
const tram = (
  <Svg>
    <Line d="M10.5 7.5l3.5-2.5-3-2.5M9.5 2.5h4" />
    <Solid d="M5.5 7.5h13a2.5 2.5 0 0 1 2.4 1.8l.9 3.2V16H2.2v-3.5l.9-3.2a2.5 2.5 0 0 1 2.4-1.8zM4.6 9.7h2.6v3H3.6zM8.4 9.7h3v3h-3zM12.6 9.7h3v3h-3zM16.8 9.7h2.6l1 3h-3.6z" />
    <Wheel x={6} y={17.8} r={1.3} />
    <Wheel x={18} y={17.8} r={1.3} />
    <Line d={RAIL} />
  </Svg>
);
/** The metro: the Prague metro's down arrow, its shaft the M. */
const metro = (
  <Svg>
    <Line d="M7.5 11.5V3l4.5 6 4.5-6v8.5H21L12 21 3 11.5z" width={2} />
  </Svg>
);
/** A trolleybus, its two poles raised behind it. */
const trolleybus = (
  <Svg>
    <Line d="M12.5 8.5L7.5 2.5M15.5 8.5l-5-6" />
    <Solid d="M2 17V10.5a2 2 0 0 1 2-2h13a2 2 0 0 1 1.8 1.1L22 15v2h-2.5a2.5 2.5 0 0 0-5 0h-5a2.5 2.5 0 0 0-5 0zM4 10.5h11v2.8H4zM16.5 10.5h1.2l1.6 2.8h-2.8z" />
    <Wheel x={7} y={17} r={1.8} />
    <Wheel x={17} y={17} r={1.8} />
  </Svg>
);
/** A bus, its windscreen to the right. */
const bus = (
  <Svg>
    <Solid d="M2 17V6a2 2 0 0 1 2-2h13a2 2 0 0 1 1.8 1.1L22 12v5h-2.5a2.5 2.5 0 0 0-5 0h-5a2.5 2.5 0 0 0-5 0zM4 6.5h11v4.5H4zM16.5 6.5h1.3l2.4 4.5h-3.7z" />
    <Wheel x={7} y={17} r={1.8} />
    <Wheel x={17} y={17} r={1.8} />
  </Svg>
);
/** A boat: its hull, its cabin with two windows, its mast. */
const water = (
  <Svg>
    <Line d="M12 8.5V4.5" />
    <Solid d="M7 14V9.5a1 1 0 0 1 1-1h8a1 1 0 0 1 1 1V14zM9 10.5h2.2v2H9zM12.8 10.5H15v2h-2.2z" />
    <Solid d="M2 14.5h20l-2.5 5h-15z" />
  </Svg>
);
/** A cabin hanging from its rope. */
const cable = (
  <Svg>
    <Line d="M2 5.5l20-3M12 4v3" />
    <Solid d="M8 7h8a3 3 0 0 1 3 3v8a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2v-8a3 3 0 0 1 3-3zM7 10h10v4H7z" />
  </Svg>
);

const SIGNS: Record<string, { sign: ReactNode; label: string; word: string }> = {
  rail: { sign: train, label: "vlak", word: "Vlaky" },
  metro: { sign: metro, label: "metro", word: "Metro" },
  tram: { sign: tram, label: "tramvaj", word: "Tramvaje" },
  trolleybus: { sign: trolleybus, label: "trolejbus", word: "Trolejbusy" },
  bus: { sign: bus, label: "autobus", word: "Autobusy" },
  water: { sign: water, label: "loď", word: "Lodě" },
  cable: { sign: cable, label: "lanovka", word: "Lanovky" },
  funicular: { sign: cable, label: "lanová dráha", word: "Lanovky" },
};

/** The modes calling at a stop, in the API's order (rail first), as signs. */
export function ModeIcons({
  modes,
  max = 4,
  className,
}: {
  modes: readonly string[];
  max?: number;
  className?: string;
}) {
  const known = modes.filter((m) => SIGNS[m]).slice(0, max);
  if (known.length === 0) return null;
  return (
    <span
      className={[classes.modes, className].filter(Boolean).join(" ")}
      role="img"
      aria-label={known.map((m) => SIGNS[m]!.label).join(", ")}
    >
      {known.map((m) => (
        <span key={m} className={classes.mode}>
          {SIGNS[m]!.sign}
        </span>
      ))}
    </span>
  );
}

/** What a stop of a group serves, in a word: "Vlaky", "Autobusy"; "MHD" for several of a
 * city's modes. */
export function modesWord(modes: readonly string[]): string {
  const known = modes.filter((m) => SIGNS[m]);
  if (known.length === 1) return SIGNS[known[0]!]!.word;
  if (known.includes("rail")) return "Vlaky";
  return known.length > 1 ? "MHD" : "Zastávka";
}
