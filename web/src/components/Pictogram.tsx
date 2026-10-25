import {
  IconAlertCircleFilled,
  IconArrowsLeftRight,
  IconBed,
  IconBedFlat,
  IconBike,
  IconBikeOff,
  IconBlind,
  IconBriefcase,
  IconBus,
  IconCar,
  IconCircleFilled,
  IconCoffee,
  IconDeviceTv,
  IconDisabled2,
  IconEye,
  IconGlass,
  IconInfoCircle,
  IconMail,
  IconPhone,
  IconPlane,
  IconShip,
  IconSteam,
  IconToolsKitchen2,
  IconTrain,
  IconVolumeOff,
  IconWheelchair,
  IconWifi,
  IconWoman,
  type Icon,
} from "@tabler/icons-react";
import type { ReactNode, SVGProps } from "react";
import classes from "./Pictogram.module.css";

// The signs the timetable prints for a source code, as the specs draw them: the JDF 1.11
// Tabulka pevných kódů (MD, metodický pokyn č. 5, příloha 2) and the značky of the CZPTT central
// notes (SŽ, Popis datové komunikace IS KADR, Doplněk číselníky 1.04, 2.16). Keyed literally by
// `system:code`. Typographic signs are text; drawn ones are Tabler icons where Tabler has the
// shape, small SVGs where it does not. `order` is the print order: the KADR Priorita, and the
// JDF table's order with the days of operation in week order (X, 1–7, +).
//
// Two tiers. The main signs are what a passenger picks a train or bus by: accessibility,
// bicycles, classes, sleeping, catering, power, wifi, quiet, and what changes the ride
// (reservation needed, on request, a change or a replacement bus). Of these, the plain ones are
// badges, their detail (a wheelchair booking, say) in the legend; the others need their text.
// Everything else is detail, on request.

type Frame = "circle" | "box" | "filled" | "struck";

interface Pictogram {
  sign: ReactNode;
  frame?: Frame;
  order: number;
}

const ICON = { size: 16, stroke: 1.75 } as const;
const icon = (I: Icon) => <I {...ICON} />;

function Svg({ children, ...props }: SVGProps<SVGSVGElement>) {
  return (
    <svg
      viewBox="0 0 24 24"
      width={ICON.size}
      height={ICON.size}
      fill="none"
      stroke="currentColor"
      strokeWidth={ICON.stroke}
      strokeLinecap="round"
      strokeLinejoin="round"
      {...props}
    >
      {children}
    </svg>
  );
}

/** ⚒: jede v pracovních dnech. */
const hammer = (
  <>
    <path d="M12 8.5V21" strokeWidth={2.25} />
    <path d="M9 3.5h6.5a1 1 0 0 1 1 1V7a1 1 0 0 1-1 1H11L9 6.5z" fill="currentColor" />
  </>
);
const hammers = (
  <Svg strokeWidth={0}>
    <g transform="rotate(-42 12 14)" stroke="currentColor">
      {hammer}
    </g>
    <g transform="rotate(42 12 14) translate(24 0) scale(-1 1)" stroke="currentColor">
      {hammer}
    </g>
  </Svg>
);
/** ≀: spoj jede po jiné trase. */
const detour = (
  <Svg>
    <path d="M12 3c-4 3 4 6 0 9s4 6 0 9" />
  </Svg>
);
/** ◖ and ◗: only to set down, only to pick up. */
const halfDisc = (side: "left" | "right") => (
  <Svg>
    <path
      d={side === "left" ? "M13 5a7 7 0 0 0 0 14z" : "M11 5a7 7 0 0 1 0 14z"}
      fill="currentColor"
    />
  </Svg>
);
/** ◇: nutno přestoupit. */
const change = (
  <Svg>
    <path d="M12 5l6 7-6 7-6-7z" />
  </Svg>
);
/** Two pins in a circle: a 230 V socket. */
const socket = (
  <Svg>
    <circle cx={12} cy={12} r={8} />
    <circle cx={9} cy={12} r={1.25} fill="currentColor" />
    <circle cx={15} cy={12} r={1.25} fill="currentColor" />
  </Svg>
);
/** A coach from the side: přímý vůz. */
const coach = (
  <Svg>
    <rect x={3} y={6} width={18} height={10} rx={2} />
    <path d="M3 11h18M7 6v5M12 6v5M17 6v5" />
    <circle cx={7} cy={18.5} r={1.25} />
    <circle cx={17} cy={18.5} r={1.25} />
  </Svg>
);
const withWheelchair = (text: string) => (
  <>
    {text}
    <IconWheelchair size={12} stroke={2} />
  </>
);
const paragraph = (n: string) => (
  <>
    §<sub>{n}</sub>
  </>
);

const JDF: [string, Omit<Pictogram, "order">][] = [
  ["X", { sign: hammers }],
  ...["1", "2", "3", "4", "5", "6", "7"].map((d): [string, Omit<Pictogram, "order">] => [
    d,
    { sign: d, frame: "circle" },
  ]),
  ["+", { sign: "†" }],
  ["R", { sign: "R" }],
  ["#", { sign: "R", frame: "box" }],
  ["|", { sign: "|" }],
  ["<", { sign: detour }],
  ["@", { sign: icon(IconWheelchair) }],
  ["%", { sign: icon(IconToolsKitchen2) }],
  ["W", { sign: "WC" }],
  ["w", { sign: withWheelchair("WC") }],
  ["x", { sign: "×" }],
  ["~", { sign: "MHD" }],
  ["(", { sign: halfDisc("left") }],
  [")", { sign: halfDisc("right") }],
  ["$", { sign: "CLO" }],
  ["{", { sign: icon(IconDisabled2) }],
  ["}", { sign: icon(IconBlind) }],
  ["[", { sign: icon(IconBriefcase) }],
  ["O", { sign: icon(IconBike) }],
  ["v", { sign: icon(IconTrain) }],
  ["§", { sign: "§" }],
  ["A", { sign: paragraph("1") }],
  ["B", { sign: paragraph("2") }],
  ["C", { sign: paragraph("3") }],
  ["T", { sign: icon(IconPhone) }],
  ["!", { sign: icon(IconAlertCircleFilled) }],
  ["t", { sign: withWheelchair("T") }],
  ["b", { sign: icon(IconBus) }],
  ["U", { sign: "M" }],
  ["S", { sign: icon(IconShip) }],
  ["J", { sign: icon(IconPlane) }],
  ["P", { sign: "P+R", frame: "filled" }],
];

const CENTRAL_NOTES: [string, Pictogram][] = [
  ["10", { sign: "1.2.", order: 300 }],
  ["11", { sign: "1.", order: 310 }],
  ["12", { sign: "2.", order: 320 }],
  ["13", { sign: icon(IconBed), order: 340 }],
  ["14", { sign: icon(IconBedFlat), order: 350 }],
  ["15", { sign: coach, order: 360 }],
  ["16", { sign: icon(IconCar), order: 370 }],
  ["17", { sign: icon(IconWheelchair), order: 400 }],
  ["18", { sign: icon(IconToolsKitchen2), order: 410 }],
  ["19", { sign: "R", order: 440 }],
  ["20", { sign: "R", frame: "box", order: 450 }],
  ["21", { sign: icon(IconBriefcase), order: 470 }],
  ["22", { sign: icon(IconBike), order: 500 }],
  ["23", { sign: change, order: 702 }],
  ["24", { sign: icon(IconGlass), order: 430 }],
  ["25", { sign: icon(IconCoffee), order: 420 }],
  ["26", { sign: <IconBriefcase size={12} stroke={2} />, frame: "circle", order: 480 }],
  ["27", { sign: <IconBriefcase size={12} stroke={2} />, frame: "box", order: 490 }],
  ["28", { sign: <IconBike size={12} stroke={2} />, frame: "circle", order: 510 }],
  ["29", { sign: <IconBike size={12} stroke={2} />, frame: "box", order: 520 }],
  ["30", { sign: socket, order: 540 }],
  ["32", { sign: icon(IconBus), order: 590 }],
  ["33", { sign: "D", order: 460 }],
  ["34", { sign: <IconWheelchair size={14} stroke={2} />, frame: "filled", order: 390 }],
  ["35", { sign: icon(IconEye), order: 330 }],
  ["36", { sign: icon(IconBikeOff), order: 530 }],
  ["37", { sign: icon(IconSteam), order: 995 }],
  ["38", { sign: icon(IconWoman), order: 560 }],
  ["39", { sign: icon(IconVolumeOff), order: 550 }],
  ["40", { sign: icon(IconWifi), order: 570 }],
  ["41", { sign: icon(IconDeviceTv), order: 580 }],
  ["42", { sign: "kino", order: 620 }],
  ["43", { sign: icon(IconCircleFilled), order: 690 }],
  ["44", { sign: icon(IconArrowsLeftRight), order: 1000 }],
  ["45", { sign: icon(IconMail), order: 630 }],
  ["46", { sign: "WC", frame: "struck", order: 640 }],
];

const PICTOGRAMS = new Map<string, Pictogram>([
  ...JDF.map(([code, p], n): [string, Pictogram] => [`jdf:${code}`, { ...p, order: n * 10 }]),
  ...CENTRAL_NOTES.map(([code, p]): [string, Pictogram] => [`czptt_note:${code}`, p]),
]);

const PLAIN = new Set([
  ...["@", "{", "%", "O"].map((c) => `jdf:${c}`),
  ...[
    "10",
    "11",
    "12",
    "13",
    "14",
    "17",
    "18",
    "22",
    "24",
    "25",
    "28",
    "30",
    "34",
    "36",
    "39",
    "40",
    "46",
  ].map((c) => `czptt_note:${c}`),
]);
const STATED = new Set([
  ...["#", "<", "T", "!"].map((c) => `jdf:${c}`),
  ...["20", "23", "27", "29", "32", "43"].map((c) => `czptt_note:${c}`),
]);

/** How a code shows: `plain` as its sign alone, `stated` as its sign with its text, `detail`
 * only on request. A code the table does not know is detail. */
export type Tier = "plain" | "stated" | "detail";

export function pictogramTier(key: string): Tier {
  return PLAIN.has(key) ? "plain" : STATED.has(key) ? "stated" : "detail";
}

/** Where a code goes among the others; a code the table does not know goes last. */
export function pictogramOrder(key: string): number {
  return PICTOGRAMS.get(key)?.order ?? Number.MAX_SAFE_INTEGER;
}

export function hasPictogram(key: string): boolean {
  return PICTOGRAMS.has(key);
}

/** The sign of a `system:code`; a code without one gets a plain info mark. */
export function CodeSign({ code, className }: { code: string; className?: string }) {
  const p = PICTOGRAMS.get(code);
  const frame = p?.frame ? classes[p.frame] : undefined;
  return (
    <span className={[classes.sign, frame, className].filter(Boolean).join(" ")} aria-hidden>
      {p ? p.sign : icon(IconInfoCircle)}
    </span>
  );
}
