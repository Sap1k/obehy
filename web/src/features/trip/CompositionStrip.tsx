import { Popover, Skeleton, UnstyledButton } from "@mantine/core";
import {
  IconArrowNarrowLeft,
  IconBabyCarriage,
  IconBed,
  IconBike,
  IconBriefcase,
  IconCoffee,
  IconLuggage,
  IconMovie,
  IconPlug,
  IconSofa,
  IconToolsKitchen2,
  IconVolumeOff,
  IconWheelchair,
  IconWifi,
  IconWoman,
  type Icon,
} from "@tabler/icons-react";
import { useState } from "react";
import type { Trip, TripCall } from "../../api/client";
import { ago } from "../../lib/hooks";
import {
  cabEnds,
  callName,
  classLabel,
  featureText,
  partOfSegment,
  segmentAt,
  shownSegments,
  unitName,
  unitsOf,
  type CompositionUnit,
  type Ends,
} from "./composition";
import { useComposition } from "./queries";
import { isPassed } from "./runCalls";
import classes from "./CompositionStrip.module.css";
import panel from "./TripPanel.module.css";

// The vocabulary's icon names (data/vehicles/attributes.toml) of the features a coach shows.
const ICONS: Record<string, Icon> = {
  wifi: IconWifi,
  power_socket: IconPlug,
  wheelchair: IconWheelchair,
  wheelchair_lift: IconWheelchair,
  bicycle_reservation: IconBike,
  family: IconBabyCarriage,
  cinema: IconMovie,
  open_saloon: IconSofa,
  bicycle: IconBike,
  luggage: IconLuggage,
  couchette: IconBed,
  sleeper: IconBed,
  restaurant: IconToolsKitchen2,
  refreshments: IconCoffee,
  quiet: IconVolumeOff,
  business: IconBriefcase,
  ladies: IconWoman,
};
// On the coach itself: the ones a rider looks for, at most three; the rest are in its detail.
const ON_COACH = [
  "sleeper",
  "couchette",
  "restaurant",
  "wheelchair_lift",
  "wheelchair",
  "bicycle_reservation",
  "bicycle",
  "family",
  "quiet",
  "power_socket",
];

/** "Řazení vlaku": a card in the vehicle section, beside the vehicle's own card: the train's
 * vehicles in order for one stretch, the one a rider boards in (else the one it is in now),
 * with a vehicle's detail on a tap. Only for a trip a source answers for; nothing while there
 * is no answer to show. `heading`: no vehicle card above it, so it brings the section's
 * heading itself. */
export function CompositionStrip({
  trip,
  boarded,
  now,
  heading = false,
}: {
  trip: Trip;
  /** The call one boards at (of this trip or a later part of its run). */
  boarded: TripCall | undefined;
  now: number;
  heading?: boolean;
}) {
  // Asked from the board's stop when it is a call of this part of the run.
  const ours = boarded && trip.calls.some((c) => c.sequence === boarded.sequence);
  const from = ours ? boarded.location_id : undefined;
  const composition = useComposition(trip, from);
  const [chosen, setChosen] = useState<number | null>(null);
  // The vehicle whose detail is open, in the answer it was opened on: a newer answer closes it.
  const [opened, setOpen] = useState<string | null>(null);
  const data = composition.data;

  if (!composition.enabled || composition.isError) return null;
  // Given up waiting: an older answer still shows (with its age), nothing else does.
  if (composition.gaveUp && !data?.units.length) return null;
  // Pending with nothing known yet: a placeholder. An older answer (from another station, or
  // earlier) shows while the new one comes, and is replaced when it does.
  const frame = (card: React.ReactNode) =>
    heading ? (
      <section className={panel.section}>
        <h3 className={panel.sectionTitle}>Vozidlo</h3>
        {card}
      </section>
    ) : (
      card
    );
  if (!data || (data.status === "pending" && data.units.length === 0)) {
    return frame(
      <div className={classes.card} aria-busy>
        <span className={classes.title}>Řazení vlaku</span>
        <Skeleton height={44} radius="sm" />
        <span className={classes.caption}>Zjišťuji řazení…</span>
      </div>,
    );
  }
  if (data.status === "not_available" || data.segments.length === 0) return null;

  const at = ours ? boarded.sequence : trip.calls.find((c) => !isPassed(c, now))?.sequence;
  // Segments running the same train are shown as one; of its members, the one where the rider
  // is gives the order shown.
  const shown = shownSegments(data);
  const here = segmentAt(data, at);
  const index = Math.min(
    chosen ??
      Math.max(
        0,
        shown.findIndex((s) => s.members.includes(here)),
      ),
    shown.length - 1,
  );
  const stretch = shown[index]!;
  const member = stretch.members.includes(here) ? here : stretch.members[0]!;
  // The stretch's names, and its member's own span for what runs only part of it.
  const segment = { ...data.segments[member]!, ...stretch };
  const own = data.segments[member]!;
  const units = unitsOf(data, own);
  const checking = data.status === "pending" || data.status === "stale";
  const answer = `${data.answered_at}:${index}`;
  const ends = cabEnds(units);
  const sources = [...new Set(units.map((u) => u.label?.cs ?? u.source))].join(", ");

  return frame(
    <div className={classes.card}>
      <div className={classes.head}>
        <span className={classes.title}>Řazení vlaku</span>
        <span className={classes.direction}>
          <IconArrowNarrowLeft size={14} stroke={2} aria-hidden />
          směr jízdy
        </span>
      </div>
      {shown.length > 1 ? (
        <div className={classes.segments} role="tablist" aria-label="Úsek trasy">
          {shown.map((s, n) => (
            <UnstyledButton
              key={`${s.from_call.sequence}-${s.to_call.sequence}`}
              role="tab"
              aria-selected={n === index}
              className={classes.segment}
              onClick={() => {
                setChosen(n);
                setOpen(null);
              }}
            >
              {callName(trip, s.from_call)} – {callName(trip, s.to_call)}
            </UnstyledButton>
          ))}
        </div>
      ) : (
        <span className={classes.caption}>
          {callName(trip, segment.from_call)} – {callName(trip, segment.to_call)}
        </span>
      )}
      <ol className={classes.strip} aria-label="Vozy od čela vlaku">
        {units.map((unit, n) => (
          <li key={`${unit.source}-${unit.sequence}`}>
            <Popover
              opened={opened === `${answer}:${n}`}
              onChange={(opened) => !opened && setOpen(null)}
              position="bottom"
              withArrow
              shadow="md"
              width={300}
              classNames={{ dropdown: classes.popover }}
            >
              <Popover.Target>
                <UnstyledButton
                  className={classes.unit}
                  data-kind={unit.kind}
                  data-first-class={unit.classes.includes(1) || undefined}
                  data-part={partOfSegment(unit, own) || undefined}
                  aria-pressed={opened === `${answer}:${n}`}
                  aria-label={unitName(unit)}
                  onClick={() => setOpen(opened === `${answer}:${n}` ? null : `${answer}:${n}`)}
                >
                  <Glyph unit={unit} ends={ends[n]!} />
                </UnstyledButton>
              </Popover.Target>
              <Popover.Dropdown>
                <UnitDetail trip={trip} unit={unit} partial={partOfSegment(unit, own)} />
              </Popover.Dropdown>
            </Popover>
            {unit.destination && <span className={classes.through}>{unit.destination}</span>}
          </li>
        ))}
      </ol>
      <span className={classes.foot}>
        {sources}
        {checking &&
          (composition.gaveUp && data.answered_at
            ? ` · zjištěno ${ago(data.answered_at, now)}`
            : " · ověřuji…")}
      </span>
    </div>,
  );
}

// A vehicle's outline: a rectangle, slanted at a cab end (front: the left). Drawn at its size,
// inset by half the stroke so the outline, and the selection with it, stays inside.
const W = 80;
const H = 44;
const SLANT = 14;

function outline({ front, rear }: Ends): string {
  const i = 1.5;
  const mid = H * 0.55;
  const left = front ? `M${i + SLANT},${i} ` : `M${i},${i} `;
  const right = rear ? `H${W - i - SLANT} L${W - i},${mid} V${H - i} ` : `H${W - i} V${H - i} `;
  const bottom = front ? `H${i} V${mid} Z` : `H${i} Z`;
  return left + right + bottom;
}

function Glyph({ unit, ends }: { unit: CompositionUnit; ends: Ends }) {
  const shape = (
    <svg className={classes.shape} viewBox={`0 0 ${W} ${H}`} aria-hidden>
      <path d={outline(ends)} />
    </svg>
  );
  if (unit.kind === "locomotive") return shape;
  const cls = classLabel(unit);
  const icons = ON_COACH.flatMap((name) =>
    unit.features.some((f) => f.icon === name) ? [ICONS[name]!] : [],
  )
    .filter((Icon, n, all) => all.indexOf(Icon) === n)
    .slice(0, 3);
  return (
    <>
      {shape}
      <span
        className={classes.face}
        data-front={ends.front || undefined}
        data-rear={ends.rear || undefined}
      >
        <span className={classes.top}>
          <span className={classes.number}>{unit.coach_number ?? ""}</span>
          <span className={classes.class}>{cls ?? "–"}</span>
        </span>
        <span className={classes.icons} aria-hidden>
          {icons.map((Icon, n) => (
            <Icon key={n} size={12} stroke={2} />
          ))}
        </span>
      </span>
    </>
  );
}

function UnitDetail({
  trip,
  unit,
  partial,
}: {
  trip: Trip;
  unit: CompositionUnit;
  partial: boolean;
}) {
  const label = unit.details.label;
  // A through coach: where it leaves this train, and as which train it goes on (ČD).
  const leaves = unit.details.direct_coach_leaves_at;
  const next = unit.details.direct_coach_train;
  const through = [leaves && `odpojení: ${leaves}`, next && `dál jako ${next}`]
    .filter(Boolean)
    .join(", ");
  return (
    <div className={classes.detail}>
      <div className={classes.detailHead}>
        <strong>{unitName(unit)}</strong>
        {unit.vehicle_type && <span className={classes.caption}>{unit.vehicle_type}</span>}
      </div>
      {/* ČD's own description repeats the features; alone it says what the coach is. */}
      {label && unit.kind !== "locomotive" && unit.features.length === 0 && (
        <p className={classes.detailText}>{label}</p>
      )}
      {unit.destination && (
        <p className={classes.detailText}>
          Přímý vůz do {unit.destination}
          {through && ` (${through})`}
        </p>
      )}
      {unit.details.notes && <p className={classes.detailText}>{unit.details.notes}</p>}
      {partial && (
        <p className={classes.detailText}>
          Jen v úseku {callName(trip, unit.from_call)} – {callName(trip, unit.to_call)}
        </p>
      )}
      {unit.features.length > 0 && (
        <ul className={classes.features}>
          {unit.features.map((f) => {
            const Icon = f.icon ? ICONS[f.icon] : undefined;
            return (
              <li key={f.feature}>
                {Icon && <Icon size={16} stroke={2} aria-hidden />}
                {featureText(f)}
              </li>
            );
          })}
        </ul>
      )}
    </div>
  );
}
