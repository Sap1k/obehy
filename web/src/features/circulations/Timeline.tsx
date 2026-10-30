import { ActionIcon, Popover, Skeleton } from "@mantine/core";
import { IconX } from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import {
  ApiError,
  type ChainJourney,
  type Line,
  type ScopedChain,
  type VehicleAttributeValue,
} from "../../api/client";
import { LineBadge, lineLabel } from "../../components/LineBadge";
import { usePhone } from "../../lib/layout";
import { plateText } from "../../lib/plates";
import { formatClock } from "../../lib/time";
import { useVehicleDetail } from "./queries";
import {
  hourLabel,
  hourTicks,
  percent,
  type Span,
  type TimelineBar,
  type TimelineRow,
  shortRoute,
  vehicleModel,
  vehicleName,
  windowOf,
} from "./layout";
import classes from "./CirculationsPage.module.css";

/** Width of an hour on the axis, room for a short journey's ends; the day scrolls sideways. */
const HOUR_PX = 160;
/** A row's and the axis's height, as the stylesheet sets them: rows are drawn only near the
 * view, a big operator's day being thousands of them. */
const ROW_PX = 44;
const AXIS_PX = 24;
const SPARE_ROWS = 8;

function hex(colour: string | null | undefined): string | undefined {
  if (!colour) return undefined;
  return colour.startsWith("#") ? colour : `#${colour}`;
}

export function journeyTitle(j: ChainJourney): string {
  const line = j.line ? lineLabel(j.line) : (j.route_name ?? "?");
  const route = j.origin && j.destination ? `${j.origin} → ${j.destination}` : (j.headsign ?? "");
  const times =
    j.departure && j.arrival
      ? `${formatClock(j.departure)}–${formatClock(j.arrival)}`
      : `viděn ${formatClock(j.first_seen)}–${formatClock(j.last_seen)}`;
  return [line, route, times].filter(Boolean).join(" · ");
}

/** The scroller's offset and height, for drawing only the rows in view. */
function useView() {
  const ref = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ top: 0, height: 800 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    let frame = 0;
    const measure = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() =>
        setView((v) =>
          v.top === el.scrollTop && v.height === el.clientHeight
            ? v
            : { top: el.scrollTop, height: el.clientHeight },
        ),
      );
    };
    measure();
    const resized = new ResizeObserver(measure);
    resized.observe(el);
    el.addEventListener("scroll", measure, { passive: true });
    return () => {
      cancelAnimationFrame(frame);
      resized.disconnect();
      el.removeEventListener("scroll", measure);
    };
  }, []);
  return [ref, view] as const;
}

/** The day: the hours across the top, a row per chain, a bar per journey. */
export function Timeline({
  rows,
  span,
  now,
  today,
  sourceLabel,
  selected,
  onSelect,
}: {
  rows: TimelineRow[];
  span: Span;
  /** Seconds of the service day now, when the date is today. */
  now: number | null;
  today: boolean;
  sourceLabel: string;
  /** `<row key>#<bar index>` of the bar opened, `<row key>#vozidlo` of the vehicle. */
  selected: string | null;
  onSelect: (key: string | null) => void;
}) {
  const hours = (span.end - span.start) / 3600;
  const nowAt = now !== null && now >= span.start && now <= span.end ? percent(now, span) : null;
  const [scroller, view] = useView();
  const { first, last } = windowOf(view.top, view.height, rows.length, {
    rowPx: ROW_PX,
    headPx: AXIS_PX,
    spare: SPARE_ROWS,
  });
  return (
    <div ref={scroller} className={classes.scroller}>
      <div
        className={classes.canvas}
        style={{
          minWidth: `calc(var(--label-w) + ${hours * HOUR_PX}px)`,
          ["--hour" as string]: `${100 / hours}%`,
        }}
      >
        {nowAt !== null && (
          <span className={classes.now} style={{ ["--at" as string]: String(nowAt) }} aria-hidden />
        )}
        <div className={classes.axisRow} aria-hidden>
          <div className={classes.corner} />
          <div className={classes.ticks}>
            {hourTicks(span).map((h) => (
              <span
                key={h}
                className={classes.hour}
                style={{ left: `${percent(h * 3600, span)}%` }}
              >
                {hourLabel(h)}
              </span>
            ))}
          </div>
        </div>
        <ol
          className={classes.rows}
          style={{
            paddingTop: first * ROW_PX,
            paddingBottom: (rows.length - last) * ROW_PX,
          }}
        >
          {rows.slice(first, last).map((row) => (
            <li key={row.key} className={classes.row}>
              <VehicleLabel
                chain={row.chain}
                opened={selected === `${row.key}#vozidlo`}
                onOpen={(opened) => onSelect(opened ? `${row.key}#vozidlo` : null)}
                today={today}
                sourceLabel={sourceLabel}
              />
              <div className={classes.lane}>
                {row.bars.map((bar, i) => {
                  const left = percent(bar.start, span);
                  const width = percent(bar.end, span) - left;
                  const line = bar.journey.line;
                  const key = `${row.key}#${i}`;
                  const button = (
                    <button
                      key={i}
                      type="button"
                      className={classes.bar}
                      data-out={!bar.inScope || undefined}
                      data-highlighted={bar.highlighted || undefined}
                      data-selected={selected === key || undefined}
                      style={{
                        left: `${left}%`,
                        width: `${width}%`,
                        background: hex(line?.color),
                        color: hex(line?.text_color),
                      }}
                      title={selected === key ? undefined : journeyTitle(bar.journey)}
                      onClick={() => onSelect(selected === key ? null : key)}
                    >
                      <span className={classes.barLine}>
                        {line ? lineLabel(line) : bar.journey.route_name}
                      </span>
                      {bar.journey.origin && bar.journey.destination && (
                        <span className={classes.barRoute}>
                          {shortRoute(bar.journey.origin, bar.journey.destination)}
                        </span>
                      )}
                    </button>
                  );
                  // Only the bar opened has a popover: a day has thousands of bars.
                  if (selected !== key) return button;
                  return (
                    <Popover
                      key={i}
                      opened
                      onChange={(opened) => !opened && onSelect(null)}
                      position="bottom-start"
                      withArrow
                      shadow="md"
                      width={320}
                    >
                      <Popover.Target>{button}</Popover.Target>
                      <Popover.Dropdown className={classes.popover}>
                        <JourneyCard
                          bar={bar}
                          vehicleId={today ? row.chain.vehicle_id : null}
                          onClose={() => onSelect(null)}
                        />
                      </Popover.Dropdown>
                    </Popover>
                  );
                })}
              </div>
            </li>
          ))}
        </ol>
      </div>
    </div>
  );
}

/** A journey opened on the timeline: its line and ends, the times, the break before it; on
 * today's page, the vehicle on the map. */
function JourneyCard({
  bar,
  vehicleId,
  onClose,
}: {
  bar: TimelineBar;
  vehicleId: string | null;
  onClose: () => void;
}) {
  const j = bar.journey;
  const facts = [
    j.departure && j.arrival
      ? `jízdní řád ${formatClock(j.departure)}–${formatClock(j.arrival)}`
      : null,
    `viděn ${formatClock(j.first_seen)}–${formatClock(j.last_seen)}`,
    j.layover_s !== null ? `přestávka ${Math.round(j.layover_s / 60)} min` : null,
  ].filter(Boolean);
  return (
    <section className={classes.card} aria-label="Spoj">
      <div className={classes.cardHead}>
        {j.line ? <LineBadge line={j.line} /> : j.route_name && <strong>{j.route_name}</strong>}
        <span className={classes.cardRoute}>
          {j.origin && j.destination ? `${j.origin} → ${j.destination}` : journeyTitle(j)}
        </span>
        <ActionIcon variant="subtle" color="gray" size="sm" onClick={onClose} aria-label="Zavřít">
          <IconX size={16} stroke={2} />
        </ActionIcon>
      </div>
      <div className={classes.cardFacts}>
        {facts.map((f) => (
          <span key={f}>{f}</span>
        ))}
        {vehicleId && (
          <Link to="/vozidlo/$vehicleId" params={{ vehicleId }} className={classes.cardLink}>
            Na mapě
          </Link>
        )}
      </div>
    </section>
  );
}

/** The row's vehicle: its fleet number, and under it its model, when the registry knows it;
 * else the source's ID. Opens what we know of it. */
function VehicleLabel({
  chain,
  opened,
  onOpen,
  today,
  sourceLabel,
}: {
  chain: ScopedChain;
  opened: boolean;
  onOpen: (opened: boolean) => void;
  today: boolean;
  sourceLabel: string;
}) {
  const name = vehicleName(chain);
  const model = vehicleModel(chain);
  const label = (
    <button
      type="button"
      className={classes.label}
      data-selected={opened || undefined}
      aria-label={`Vozidlo ${name}`}
      onClick={() => onOpen(!opened)}
    >
      <span className={classes.labelName}>{name}</span>
      {model && <span className={classes.labelModel}>{model}</span>}
    </button>
  );
  // Only the vehicle opened has a popover, as with the bars.
  if (!opened) return label;
  return (
    <VehiclePopover label={label} onClose={() => onOpen(false)}>
      <VehicleCard
        chain={chain}
        today={today}
        sourceLabel={sourceLabel}
        onClose={() => onOpen(false)}
      />
    </VehiclePopover>
  );
}

/** Beside the column; on a phone, whose column leaves no room beside it, under the vehicle. */
function VehiclePopover({
  label,
  onClose,
  children,
}: {
  label: ReactNode;
  onClose: () => void;
  children: ReactNode;
}) {
  const phone = usePhone();
  return (
    <Popover
      opened
      onChange={(o) => !o && onClose()}
      position={phone ? "bottom-start" : "right-start"}
      withArrow
      shadow="md"
      width={320}
    >
      <Popover.Target>{label}</Popover.Target>
      <Popover.Dropdown className={classes.popover}>{children}</Popover.Dropdown>
    </Popover>
  );
}

function journeysWord(n: number): string {
  if (n === 1) return "1 spoj";
  return n >= 2 && n <= 4 ? `${n} spoje` : `${n} spojů`;
}

/** Shown in the card's head; the vehicle's other attributes are listed. */
const IN_HEAD = new Set(["fleet_number", "manufacturer", "model"]);

function attributeValue(a: VehicleAttributeValue): string {
  if (a.value === "true") return "ano";
  if (a.value === "false") return "ne";
  return a.attribute === "licence_plate" ? plateText(a.value) : a.value;
}

/** A vehicle opened on the timeline: what the row says of its day, then what the registry and
 * the sources say of it; on today's page, the vehicle on the map while it reports. */
function VehicleCard({
  chain,
  today,
  sourceLabel,
  onClose,
}: {
  chain: ScopedChain;
  today: boolean;
  sourceLabel: string;
  onClose: () => void;
}) {
  const detail = useVehicleDetail(chain.vehicle_id, true);
  const model = vehicleModel(chain);
  const lines = new Map<string, Line>();
  for (const j of chain.journeys) if (j.line) lines.set(j.line.route_id, j.line);
  const unknown = detail.error instanceof ApiError && detail.error.status === 404;
  const facts: [string, ReactNode][] = [
    [
      "Oběh",
      `${journeysWord(chain.journeys.length)}, ${formatClock(chain.first_seen)}–${formatClock(chain.last_seen)}`,
    ],
    [sourceLabel, chain.source_vehicle_id],
  ];
  if (chain.vehicle_id !== `${chain.source}:${chain.source_vehicle_id}`) {
    facts.push(["Naše ID", chain.vehicle_id]);
  }
  for (const a of detail.data?.attributes ?? []) {
    if (!IN_HEAD.has(a.attribute)) facts.push([a.label?.cs ?? a.attribute, attributeValue(a)]);
  }
  const reports = [
    ...new Set((detail.data?.reports ?? []).map((r) => r.label?.cs ?? r.source)),
  ].join(", ");
  if (reports) facts.push(["Teď hlásí", reports]);
  if (detail.data?.registry?.datasets.length) {
    facts.push(["Registr", detail.data.registry.datasets.join(", ")]);
  }
  return (
    <section className={classes.card} aria-label="Vozidlo">
      <div className={classes.cardHead}>
        <span className={classes.cardTitle}>
          <span className={classes.cardRoute}>{vehicleName(chain)}</span>
          {model && <span className={classes.cardModel}>{model}</span>}
        </span>
        <ActionIcon variant="subtle" color="gray" size="sm" onClick={onClose} aria-label="Zavřít">
          <IconX size={16} stroke={2} />
        </ActionIcon>
      </div>
      {lines.size > 0 && (
        <div className={classes.cardLines}>
          {[...lines.values()].map((l) => (
            <LineBadge key={l.route_id} line={l} />
          ))}
        </div>
      )}
      <dl className={classes.cardList}>
        {facts.map(([term, value]) => (
          <div key={term}>
            <dt>{term}</dt>
            <dd>{value}</dd>
          </div>
        ))}
      </dl>
      {detail.isPending && <Skeleton height={18} />}
      {unknown && (
        <p className={classes.cardNote}>Registr ho nezná a teď ho žádný zdroj nehlásí.</p>
      )}
      {detail.isError && !unknown && (
        <p className={classes.cardNote}>Detail se nepodařilo načíst.</p>
      )}
      {today && detail.data?.vehicle && (
        <Link
          to="/vozidlo/$vehicleId"
          params={{ vehicleId: chain.vehicle_id }}
          className={classes.cardLink}
        >
          Na mapě
        </Link>
      )}
    </section>
  );
}
