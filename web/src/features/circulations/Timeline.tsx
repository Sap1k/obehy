import { ActionIcon, Popover, Skeleton } from "@mantine/core";
import { IconArrowsHorizontal, IconMinus, IconPlus, IconX } from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
  type ReactNode,
  type RefObject,
} from "react";
import {
  ApiError,
  type ChainJourney,
  type Line,
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
  tickStep,
  vehicleModel,
  vehicleName,
  windowOf,
  ZOOM_PX,
  zoomAround,
  zoomed,
} from "./layout";
import classes from "./CirculationsPage.module.css";

/** Width of an hour on the axis at first, room for a short journey's ends; the day scrolls
 * sideways and zooms (the viewer's last zoom is kept). */
const HOUR_PX = 160;
const ZOOM_KEY = "obehy.hourPx";
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

/** The scroller's offset and size, for drawing only the rows in view, and the vehicle
 * column's width, for fitting the day. */
function useView() {
  const ref = useRef<HTMLDivElement>(null);
  const [view, setView] = useState({ top: 0, height: 800, width: 1000, label: 0 });
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    let frame = 0;
    const measure = () => {
      cancelAnimationFrame(frame);
      frame = requestAnimationFrame(() =>
        setView((v) => {
          const label = labelWidth(el);
          return v.top === el.scrollTop &&
            v.height === el.clientHeight &&
            v.width === el.clientWidth &&
            v.label === label
            ? v
            : { top: el.scrollTop, height: el.clientHeight, width: el.clientWidth, label };
        }),
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

function storedHourPx(): number {
  try {
    const px = Number(localStorage.getItem(ZOOM_KEY));
    return px >= ZOOM_PX.min && px <= ZOOM_PX.max ? px : HOUR_PX;
  } catch {
    return HOUR_PX;
  }
}

/** The vehicle column's width, as the stylesheet sets it (narrower on a phone). */
function labelWidth(scroller: HTMLElement): number {
  return scroller.querySelector<HTMLElement>(`.${classes.corner}`)?.offsetWidth ?? 0;
}

/** An hour's width: the buttons halve and double it, ctrl+wheel (a trackpad's pinch) and a
 * two-finger pinch scale it, about the time under the pointer; never narrower than the whole
 * day fits. */
function useZoom(
  ref: RefObject<HTMLDivElement | null>,
  hours: number,
  { width, label }: { width: number; label: number },
) {
  const [hourPx, setHourPx] = useState(storedHourPx);
  const anchor = useRef<{ from: number; left: number; pointerX: number } | null>(null);
  const fitPx = Math.max(1, (width - label) / hours);
  const shown = Math.max(hourPx, fitPx);
  // For the listeners: the zoom as last drawn.
  const current = useRef({ shown, fitPx });
  useLayoutEffect(() => {
    current.current = { shown, fitPx };
  }, [shown, fitPx]);

  const zoomTo = useCallback(
    (factor: number | "fit", pointerX?: number) => {
      const el = ref.current;
      if (!el) return;
      const { shown: from, fitPx: fit } = current.current;
      const next = factor === "fit" ? fit : zoomed(from, factor, fit);
      const lane = labelWidth(el);
      anchor.current = {
        from,
        left: factor === "fit" ? 0 : el.scrollLeft,
        pointerX: pointerX ?? lane + (el.clientWidth - lane) / 2,
      };
      setHourPx(next);
    },
    [ref],
  );

  useLayoutEffect(() => {
    const el = ref.current;
    const a = anchor.current;
    if (!el || !a) return;
    anchor.current = null;
    el.scrollLeft = zoomAround(a.left, a.pointerX, labelWidth(el), a.from, Math.max(hourPx, fitPx));
    try {
      localStorage.setItem(ZOOM_KEY, String(Math.round(hourPx)));
    } catch {
      // Not kept: the next visit starts at the default.
    }
  }, [ref, hourPx, fitPx]);

  useEffect(() => {
    const el = ref.current;
    if (!el) return;
    const x = (clientX: number) => clientX - el.getBoundingClientRect().left;
    const wheel = (e: WheelEvent) => {
      if (!e.ctrlKey) return;
      e.preventDefault();
      zoomTo(Math.exp(-e.deltaY * 0.01), x(e.clientX));
    };
    let pinch: { distance: number; px: number } | null = null;
    const spread = (t: TouchList) =>
      Math.hypot(t[0]!.clientX - t[1]!.clientX, t[0]!.clientY - t[1]!.clientY);
    const start = (e: TouchEvent) => {
      pinch =
        e.touches.length === 2 ? { distance: spread(e.touches), px: current.current.shown } : null;
    };
    const move = (e: TouchEvent) => {
      if (!pinch || e.touches.length !== 2) return;
      e.preventDefault();
      const factor = (spread(e.touches) / pinch.distance) * (pinch.px / current.current.shown);
      zoomTo(factor, x((e.touches[0]!.clientX + e.touches[1]!.clientX) / 2));
    };
    el.addEventListener("wheel", wheel, { passive: false });
    el.addEventListener("touchstart", start, { passive: true });
    el.addEventListener("touchmove", move, { passive: false });
    return () => {
      el.removeEventListener("wheel", wheel);
      el.removeEventListener("touchstart", start);
      el.removeEventListener("touchmove", move);
    };
  }, [ref, zoomTo]);

  return { hourPx: shown, fitted: shown <= fitPx + 0.5, zoomTo };
}

/** The day: the hours across the top, a row per vehicle, a bar per journey. */
export function Timeline({
  busy,
  rows,
  span,
  now,
  today,
  sourceLabel,
  selected,
  onSelect,
}: {
  /** Another scope or day is on its way: these rows are the last answer's. */
  busy: boolean;
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
  const zoom = useZoom(scroller, hours, view);
  const { first, last } = windowOf(view.top, view.height, rows.length, {
    rowPx: ROW_PX,
    headPx: AXIS_PX,
    spare: SPARE_ROWS,
  });
  return (
    <div
      ref={scroller}
      className={classes.scroller}
      data-busy={busy || undefined}
      aria-busy={busy || undefined}
    >
      <div
        className={classes.canvas}
        style={{
          width: `calc(var(--label-w) + ${hours * zoom.hourPx}px)`,
          ["--hour" as string]: `${100 / hours}%`,
        }}
      >
        {nowAt !== null && (
          <span className={classes.now} style={{ ["--at" as string]: String(nowAt) }} aria-hidden />
        )}
        <div className={classes.axisRow}>
          <div className={classes.corner}>
            <ActionIcon
              variant="subtle"
              color="gray"
              size="xs"
              aria-label="Oddálit"
              disabled={zoom.fitted}
              onClick={() => zoom.zoomTo(0.5)}
            >
              <IconMinus size={14} stroke={2} />
            </ActionIcon>
            <ActionIcon
              variant="subtle"
              color="gray"
              size="xs"
              aria-label="Celý den"
              disabled={zoom.fitted}
              onClick={() => zoom.zoomTo("fit")}
            >
              <IconArrowsHorizontal size={14} stroke={2} />
            </ActionIcon>
            <ActionIcon
              variant="subtle"
              color="gray"
              size="xs"
              aria-label="Přiblížit"
              disabled={zoom.hourPx >= ZOOM_PX.max}
              onClick={() => zoom.zoomTo(2)}
            >
              <IconPlus size={14} stroke={2} />
            </ActionIcon>
          </div>
          <div className={classes.ticks}>
            {hourTicks(span, tickStep(zoom.hourPx)).map((h) => (
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
                row={row}
                opened={selected === `${row.key}#vozidlo`}
                onOpen={(opened) => onSelect(opened ? `${row.key}#vozidlo` : null)}
                today={today}
                sourceLabel={sourceLabel}
              />
              <div className={classes.lane}>
                {row.breaks.map((b) => (
                  <span
                    key={b.start}
                    className={classes.break}
                    style={{
                      left: `${percent(b.start, span)}%`,
                      width: `${percent(b.end, span) - percent(b.start, span)}%`,
                    }}
                    aria-hidden
                  />
                ))}
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
  row,
  opened,
  onOpen,
  today,
  sourceLabel,
}: {
  row: TimelineRow;
  opened: boolean;
  onOpen: (opened: boolean) => void;
  today: boolean;
  sourceLabel: string;
}) {
  const { chain } = row;
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
        row={row}
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
  row,
  today,
  sourceLabel,
  onClose,
}: {
  row: TimelineRow;
  today: boolean;
  sourceLabel: string;
  onClose: () => void;
}) {
  const { chain, chains } = row;
  const detail = useVehicleDetail(chain.vehicle_id, true);
  const model = vehicleModel(chain);
  const lines = new Map<string, Line>();
  for (const j of row.bars) if (j.journey.line) lines.set(j.journey.line.route_id, j.journey.line);
  const unknown = detail.error instanceof ApiError && detail.error.status === 404;
  const times = chains.map((c) => `${formatClock(c.first_seen)}–${formatClock(c.last_seen)}`);
  const facts: [string, ReactNode][] = [
    [
      "Oběh",
      chains.length > 1
        ? `${journeysWord(row.bars.length)} ve ${chains.length} částech: ${times.join(", ")}`
        : `${journeysWord(row.bars.length)}, ${times[0]}`,
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
