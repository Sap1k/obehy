import { Modal, Skeleton, UnstyledButton } from "@mantine/core";
import { IconChevronRight } from "@tabler/icons-react";
import { useState } from "react";
import { ApiError, type TripHistory } from "../../api/client";
import { BoardMessage } from "../../components/BoardStates";
import {
  delayScale,
  delayWords,
  minuteTicks,
  place,
  rangeBar,
  type HistoryStop,
  type Scale,
} from "../../lib/history";
import { usePhone } from "../../lib/layout";
import { useTripHistory } from "./queries";
import classes from "./History.module.css";

// "Jak jezdí": how late the trip's journey was at each stop over the recorded days, in a
// dialog, so the trip's own view stays as it is.

const dayMonth = new Intl.DateTimeFormat("cs-CZ", { day: "numeric", month: "numeric" });

function shortDay(key: string): string {
  return dayMonth.format(new Date(`${key}T12:00:00Z`));
}

/** The link that opens the dialog; the history is asked for only once it is open. */
export function HistoryLink({ tripId }: { tripId: string }) {
  const [opened, setOpened] = useState(false);
  const phone = usePhone();
  return (
    <>
      <UnstyledButton className={classes.link} onClick={() => setOpened(true)}>
        Jak jezdí
        <IconChevronRight size={14} stroke={2} aria-hidden />
      </UnstyledButton>
      <Modal
        opened={opened}
        onClose={() => setOpened(false)}
        title="Jak jezdí"
        fullScreen={phone}
        size="lg"
        classNames={{ content: classes.dialog, header: classes.dialog, title: classes.dialogTitle }}
      >
        {opened && <HistoryBody tripId={tripId} />}
      </Modal>
    </>
  );
}

function HistoryBody({ tripId }: { tripId: string }) {
  const query = useTripHistory(tripId, true);
  if (query.isPending) {
    return (
      <div className={classes.loading} aria-busy="true" aria-label="Načítání">
        <Skeleton height={18} width="60%" />
        {[0, 1, 2, 3].map((i) => (
          <Skeleton key={i} height={40} />
        ))}
      </div>
    );
  }
  if (query.isError) {
    return query.error instanceof ApiError && query.error.status === 404 ? (
      <BoardMessage
        title="Historie není k dispozici"
        text="U tohoto spoje se průběh jízd nezaznamenává."
      />
    ) : (
      <BoardMessage
        title="Historii se nepodařilo načíst"
        text="Zkontrolujte připojení."
        action={{ label: "Zkusit znovu", onClick: () => void query.refetch(), primary: true }}
      />
    );
  }
  return <Stops history={query.data} />;
}

function Stops({ history }: { history: TripHistory }) {
  const span = `${shortDay(history.since)} – ${shortDay(history.until)}`;
  const timed = history.stops.filter((s) => s.runs > 0);
  const runs = history.days.length - history.cancelled;
  const summary = [
    span,
    `${runs} ${runs === 1 ? "jízda" : runs >= 2 && runs <= 4 ? "jízdy" : "jízd"}`,
    history.cancelled > 0 ? `${history.cancelled}× zrušeno` : null,
  ]
    .filter(Boolean)
    .join(" · ");
  if (timed.length === 0) {
    return (
      <div className={classes.body}>
        <p className={classes.summary}>{summary}</p>
        <BoardMessage
          title="Zatím žádné záznamy"
          text="Za toto období nemáme skutečné časy průjezdu zastávkami."
        />
      </div>
    );
  }
  const scale = delayScale(timed);
  return (
    <div className={classes.body}>
      <p className={classes.summary}>{summary}</p>
      <Axis scale={scale} />
      <ol className={classes.stops}>
        {history.stops.map((s, i) => (
          <StopRow key={`${s.location_id}:${i}`} stop={s} scale={scale} />
        ))}
      </ol>
      <p className={classes.legend}>
        <span className={classes.legendMedian} aria-hidden /> obvykle (medián)
        <span className={classes.legendWhisker} aria-hidden /> 9 jízd z 10
        <span className={classes.legendWorst} aria-hidden /> nejhorší
        <span className={classes.legendBand} aria-hidden /> včas
      </p>
    </div>
  );
}

function Axis({ scale }: { scale: Scale }) {
  return (
    <div className={classes.axis} aria-hidden>
      {minuteTicks(scale).map((m) => (
        <span key={m} className={classes.tick} style={{ left: `${place(m * 60, scale)}%` }}>
          {m > 0 ? `+${m}` : m < 0 ? `−${-m}` : "0"}
        </span>
      ))}
    </div>
  );
}

function StopRow({ stop, scale }: { stop: HistoryStop; scale: Scale }) {
  const bar = rangeBar(stop, scale);
  const share =
    stop.on_time_share === null ? null : `včas ${Math.round(stop.on_time_share * 100)} %`;
  return (
    <li className={classes.stop}>
      <div className={classes.stopHead}>
        <span className={classes.stopName}>{stop.name}</span>
        {share && <span className={classes.share}>{share}</span>}
      </div>
      {bar ? (
        <>
          <div className={classes.track} data-tone={bar.tone}>
            <span
              className={classes.band}
              style={{ left: `${place(-60, scale)}%`, right: `${100 - place(120, scale)}%` }}
            />
            <span className={classes.zero} style={{ left: `${bar.zero}%` }} />
            {bar.p90 !== null && (
              <span
                className={classes.whisker}
                style={{
                  left: `${Math.min(bar.median, bar.p90)}%`,
                  width: `${Math.abs(bar.p90 - bar.median)}%`,
                }}
              />
            )}
            <span
              className={classes.median}
              style={{
                left: `${Math.min(bar.zero, bar.median)}%`,
                width: `${Math.max(Math.abs(bar.median - bar.zero), 0.8)}%`,
              }}
            />
            {bar.worst !== null && (
              <span
                className={classes.worst}
                style={{ left: `${bar.worst}%` }}
                data-beyond={bar.beyond || undefined}
              />
            )}
          </div>
          <div className={classes.figures}>
            <span>obvykle {delayWords(stop.median_delay_s!)}</span>
            {stop.p90_delay_s !== null && <span>9 z 10 do {delayWords(stop.p90_delay_s)}</span>}
            {stop.max_delay_s !== null && <span>nejhůř {delayWords(stop.max_delay_s)}</span>}
            <span className={classes.runs}>{stop.runs}× změřeno</span>
          </div>
        </>
      ) : (
        <div className={classes.figures}>Bez záznamu</div>
      )}
    </li>
  );
}
