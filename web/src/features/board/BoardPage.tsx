import { Button, SegmentedControl } from "@mantine/core";
import { useNetwork } from "@mantine/hooks";
import {
  Link,
  Outlet,
  useChildMatches,
  useNavigate,
  useParams,
  useSearch,
} from "@tanstack/react-router";
import { Fragment, useEffect, useEffectEvent, useRef, useState } from "react";
import { ApiError } from "../../api/client";
import { AlertBanner } from "../../components/AlertBanner";
import { BoardMessage, BoardSkeleton, DayDivider } from "../../components/BoardStates";
import { DepartureRow, rowClasses } from "../../components/DepartureRow";
import { ModeIcons, modesWord } from "../../components/ModeIcon";
import { Panel } from "../../components/Panel";
import { TimeSelect } from "../../components/TimeSelect";
import { stopAlerts } from "../../lib/alerts";
import { useFlip } from "../../lib/flip";
import { ago, useNow } from "../../lib/hooks";
import { usePhone } from "../../lib/layout";
import { rememberStop } from "../../lib/recent";
import { platformsText } from "../../lib/stops";
import { dayDividerLabel, dayKey, formatClock, formatLongDay } from "../../lib/time";
import { boardRowKey, useBoard, useStopPlace } from "./queries";
import classes from "./BoardPage.module.css";

const PAGE = 40;
/** The API's largest board (api-v1.toml `departures.limit_max`). */
const LIMIT_MAX = 200;

export function BoardPage() {
  const { stopId } = useParams({ from: "/odjezdy/$stopId" });
  const search = useSearch({ from: "/odjezdy/$stopId" });
  const navigate = useNavigate({ from: "/odjezdy/$stopId" });
  const childMatches = useChildMatches();
  const phone = usePhone();
  const tripOpen = childMatches.length > 0;

  const arrivals = search.smer === "prijezdy";
  const from = search.cas;
  const only = search.jen;
  // Showing more applies to one scope; a new stop, direction, time or member starts from one
  // page.
  const scopeKey = `${stopId}|${arrivals}|${from ?? ""}|${only ?? ""}`;
  const [shown, setShown] = useState({ scopeKey, limit: PAGE });
  const limit = shown.scopeKey === scopeKey ? shown.limit : PAGE;
  const setLimit = (n: number) => setShown({ scopeKey, limit: n });

  const place = useStopPlace(stopId);
  const board = useBoard({ stopId, only, arrivals, from, limit });
  const network = useNetwork();
  const now = useNow(1000);

  const data = board.data;
  const offline = Boolean(data) && (board.isError || !network.online);

  // A station with the stop in front of it is one board, named after the station; it may show
  // either of them alone.
  const stop = place.data?.stop ?? data?.stop;
  const members = place.data?.group ?? [];
  const grouped = members.length > 1;
  const title = grouped ? members[0]!.name : stop?.name;

  // The search lists recently opened stops, a group as its station with all its modes.
  const group = place.data?.group;
  useEffect(() => {
    if (!stop) return;
    if (!group || group.length < 2) return rememberStop(stop);
    const modes = [...new Set(group.flatMap((m) => m.modes ?? []))];
    rememberStop({ ...stop, name: group[0]!.name, modes });
  }, [stop, group]);

  // The phone shows the trip on its own page.
  if (phone && tripOpen) return <Outlet />;

  const setScope = (next: {
    smer?: "prijezdy" | undefined;
    cas?: string | undefined;
    jen?: string | undefined;
  }) => void navigate({ search: (prev) => ({ ...prev, ...next }), replace: true });

  const subtitle = [
    formatLongDay(from ?? now),
    // The platforms are the opened stop's: shown while its board is its own.
    (!grouped || only === stopId) &&
      platformsText(place.data?.boarding_points.map((b) => b.public_code) ?? []),
  ]
    .filter(Boolean)
    .join(" · ");
  const noun = arrivals ? "příjezdy" : "odjezdy";

  return (
    <>
      <Panel label={arrivals ? "Příjezdy" : "Odjezdy"}>
        <section className={classes.board}>
          <header className={classes.header}>
            <h1 className={classes.title}>{title ?? (place.isError ? "Zastávka" : " ")}</h1>
            <p className={classes.subtitle}>{subtitle}</p>
          </header>
          <div className={classes.controls}>
            <SegmentedControl
              value={arrivals ? "arrivals" : "departures"}
              onChange={(v) => setScope({ smer: v === "arrivals" ? "prijezdy" : undefined })}
              data={[
                { value: "departures", label: "Odjezdy" },
                { value: "arrivals", label: "Příjezdy" },
              ]}
            />
            <TimeSelect value={from} now={now} onChange={(cas) => setScope({ cas })} />
          </div>
          {grouped && (
            <SegmentedControl
              className={classes.members}
              aria-label="Zastávka"
              value={only ?? ""}
              onChange={(v) => setScope({ jen: v || undefined })}
              data={[
                { value: "", label: "Vše" },
                ...members.map((m) => ({
                  value: m.location_id,
                  label: (
                    <span className={classes.member} title={m.name}>
                      <ModeIcons modes={m.modes ?? []} />
                      {modesWord(m.modes ?? [])}
                    </span>
                  ),
                })),
              ]}
            />
          )}

          {data &&
            stopAlerts(
              data.alerts,
              data.stops.map((s) => s.location_id),
            ).map((a) => <AlertBanner key={a.alert_id} alert={a} now={now} />)}

          <BoardBody
            stopId={stopId}
            arrivals={arrivals}
            from={from}
            limit={limit}
            setLimit={setLimit}
            setFrom={(cas) => setScope({ cas })}
            board={board}
            offline={offline}
            now={now}
            noun={noun}
            phone={phone}
          />
        </section>
      </Panel>
      {tripOpen && (
        <Panel label="Detail spoje" narrow flush>
          <Outlet />
        </Panel>
      )}
    </>
  );
}

function BoardBody({
  stopId,
  arrivals,
  from,
  limit,
  setLimit,
  setFrom,
  board,
  offline,
  now,
  noun,
  phone,
}: {
  stopId: string;
  arrivals: boolean;
  from: string | undefined;
  limit: number;
  setLimit: (n: number) => void;
  setFrom: (cas: string | undefined) => void;
  board: ReturnType<typeof useBoard>;
  offline: boolean;
  now: number;
  noun: string;
  phone: boolean;
}) {
  const sentinel = useRef<HTMLDivElement>(null);
  const tripMatch = useChildMatches().find(
    (m) => m.routeId === "/odjezdy/$stopId/spoj/$tripId/$date",
  );
  const openTrip = tripMatch?.params as { tripId?: string; date?: string } | undefined;
  const data = board.data;
  const hasMore = Boolean(data?.next);
  const atMax = limit >= LIMIT_MAX;

  // More rows of this answer while the API has them, else move the board's start on.
  const more = () => {
    if (!data) return;
    if (hasMore && !atMax) {
      setLimit(Math.min(limit + PAGE, LIMIT_MAX));
      return;
    }
    const last = data.rows[data.rows.length - 1];
    setFrom(hasMore && last ? last.scheduled : data.until);
  };

  // The phone loads more by scrolling to the end.
  const onEnd = useEffectEvent(more);
  const waiting = phone && hasMore && !board.isFetching;
  useEffect(() => {
    const target = sentinel.current;
    if (!waiting || !target) return;
    const observer = new IntersectionObserver((entries) => {
      if (entries.some((e) => e.isIntersecting)) onEnd();
    });
    observer.observe(target);
    return () => observer.disconnect();
  }, [waiting]);

  // Each poll, the rows that stay glide to their new places (a departed one leaves), new ones
  // fade in: the board never jumps.
  const list = useRef<HTMLDivElement>(null);
  useFlip(list, (data?.rows ?? []).map(boardRowKey).join("|"));

  if (!data) {
    if (board.isError) {
      const notFound = board.error instanceof ApiError && board.error.status === 404;
      return notFound ? (
        <BoardMessage
          title="Zastávka nenalezena"
          text="Tuhle zastávku v jízdních řádech nemáme. Zkuste ji vyhledat."
        />
      ) : (
        <BoardMessage
          title={`${noun === "odjezdy" ? "Odjezdy" : "Příjezdy"} se nepodařilo načíst`}
          text="Zkontrolujte připojení. Zkusíme to znovu automaticky."
          action={{ label: "Zkusit znovu", onClick: () => void board.refetch(), primary: true }}
        />
      );
    }
    return <BoardSkeleton />;
  }

  if (data.rows.length === 0) {
    const hours = Math.round((Date.parse(data.until) - Date.parse(from ?? data.since)) / 3_600_000);
    const within = hours <= 1 ? "V příští hodině" : `V příštích ${hours} hodinách`;
    return (
      <BoardMessage
        title={arrivals ? `${within} sem nic nepřijede` : `${within} odsud nic nejede`}
        text="Další spoj podle jízdního řádu může jet později nebo jiný den."
        action={{ label: `Zobrazit pozdější ${noun}`, onClick: () => setFrom(data.until) }}
      />
    );
  }

  const startDay = dayKey(from ?? now);
  const rows = data.rows.map((row, i) => {
    const shownAt = row.estimated ?? row.scheduled;
    const previous = data.rows[i - 1];
    const previousDay = previous ? dayKey(previous.estimated ?? previous.scheduled) : startDay;
    return { row, divider: dayKey(shownAt) !== previousDay ? dayDividerLabel(shownAt, now) : null };
  });
  return (
    <div className={`${classes.card} ob-fade-in`}>
      {offline && <div className={classes.offline}>Offline · uložená data</div>}
      <div role="list" ref={list}>
        {rows.map(({ row, divider }) => {
          const selected = openTrip?.tripId === row.trip_id && openTrip?.date === row.service_date;
          const key = boardRowKey(row);
          return (
            <Fragment key={key}>
              {divider && (
                <div data-flip={`day:${divider}`}>
                  <DayDivider label={divider} />
                </div>
              )}
              <Link
                role="listitem"
                data-flip={key}
                className={rowClasses.row}
                data-selected={selected}
                to="/odjezdy/$stopId/spoj/$tripId/$date"
                params={{ stopId, tripId: row.trip_id, date: row.service_date }}
                search={(prev) => ({ ...prev, od: row.location_id })}
              >
                <DepartureRow departure={row} offline={offline} />
              </Link>
            </Fragment>
          );
        })}
      </div>
      <footer className={classes.footer}>
        {phone && hasMore ? (
          <span className={classes.updated}>Další se načtou posunutím</span>
        ) : (
          <Button
            variant="subtle"
            onClick={more}
            loading={board.isFetching && board.isPlaceholderData}
          >
            {hasMore && !atMax ? `Zobrazit další ${noun}` : `Zobrazit pozdější ${noun}`}
          </Button>
        )}
        <span className={classes.updated}>
          {offline
            ? `Data z ${formatClock(data.generatedAt)} · obnoví se po připojení`
            : `Aktualizováno ${ago(data.generatedAt, now)}`}
        </span>
      </footer>
      {phone && hasMore && <div ref={sentinel} className={classes.sentinel} />}
    </div>
  );
}
