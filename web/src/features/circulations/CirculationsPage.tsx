import {
  ActionIcon,
  Chip,
  CloseButton,
  Group,
  MultiSelect,
  SegmentedControl,
  Select,
  Skeleton,
  TextInput,
} from "@mantine/core";
import { IconChevronLeft, IconChevronRight, IconSearch } from "@tabler/icons-react";
import { useNavigate, useSearch } from "@tanstack/react-router";
import { useMemo, useState } from "react";
import type { CirculationScopes } from "../../api/client";
import { BoardMessage } from "../../components/BoardStates";
import { modesWord } from "../../components/ModeIcon";
import { Panel } from "../../components/Panel";
import { useNow } from "../../lib/hooks";
import { addDays, dayKey, formatLongDay } from "../../lib/time";
import type { CirculationsSearch } from "../../router";
import { Timeline } from "./Timeline";
import { LinePicker } from "./LinePicker";
import { scopeComplete, useCirculations, useScopes, type Scope } from "./queries";
import { rowsOf, secondsOfDay, spanOf } from "./layout";
import classes from "./CirculationsPage.module.css";

// Oběhy: a source's vehicles on a date, each a row of the journeys it worked, scoped by company
// (one or more, then modes), by route or by one vehicle. The scope is the URL's, so a view
// links.

type By = "dopravce" | "linka" | "vozidlo";

const list = (text: string | undefined): string[] => (text ? text.split(",") : []);
const joined = (values: string[]): string | undefined =>
  values.length ? values.join(",") : undefined;

function vehiclesWord(n: number): string {
  if (n === 1) return "1 vozidlo";
  return n >= 2 && n <= 4 ? `${n} vozidla` : `${n} vozidel`;
}

/** Companies by name: one company can have several agency IDs (one per licence). */
function companies(scopes: CirculationScopes | undefined) {
  const byName = new Map<string, { ids: string[]; vehicles: number; modes: Map<string, number> }>();
  for (const a of scopes?.agencies ?? []) {
    const c = byName.get(a.name) ?? { ids: [], vehicles: 0, modes: new Map<string, number>() };
    c.ids.push(a.agency_id);
    c.vehicles += a.vehicles;
    for (const m of a.modes) c.modes.set(m.mode, (c.modes.get(m.mode) ?? 0) + m.vehicles);
    byName.set(a.name, c);
  }
  return byName;
}

export function CirculationsPage() {
  const search = useSearch({ from: "/obehy" });
  const navigate = useNavigate({ from: "/obehy" });
  const now = useNow(60_000);
  const today = dayKey(now);
  const date = search.datum ?? today;
  const set = (patch: Partial<CirculationsSearch>) =>
    void navigate({ search: (prev) => ({ ...prev, ...patch }), replace: true });

  // Without a source in the URL, the one that saw the most vehicles that day.
  const day = useScopes(search.zdroj, date);
  const source = search.zdroj ?? day.data?.sources[0]?.source;
  const scopes = useScopes(source, date);
  const sources = (day.data?.sources ?? []).map((s) => ({
    value: s.source,
    label: `${s.label?.cs ?? s.source} (${s.vehicles})`,
  }));
  if (source && !sources.some((s) => s.value === source)) {
    sources.push({ value: source, label: source });
  }
  const sourceName =
    day.data?.sources.find((s) => s.source === source)?.label?.cs ?? source ?? "Zdroj";
  const byName = useMemo(() => companies(scopes.data), [scopes.data]);
  // What the URL scopes by; with nothing chosen yet, the viewer's pick.
  const [picked, setPicked] = useState<By>("dopravce");
  const by: By = search.vozidlo
    ? "vozidlo"
    : search.linka
      ? "linka"
      : search.dopravce
        ? "dopravce"
        : picked;
  // Choices of another date or source stand in while the day's load: they decide nothing.
  const settled = scopes.data && !scopes.isPlaceholderData;

  // The URL's companies that the source has that day: others drop out of the scope.
  const known = new Set([...byName.values()].flatMap((c) => c.ids));
  const agencyIds = list(search.dopravce).filter((id) => !settled || known.has(id));
  const chosen = [...byName].filter(([, c]) => c.ids.some((id) => agencyIds.includes(id)));
  const modesOffered = new Map<string, number>();
  for (const [, c] of chosen) {
    for (const [m, n] of c.modes) modesOffered.set(m, (modesOffered.get(m) ?? 0) + n);
  }
  const modes = list(search.druh).filter((m) => !settled || modesOffered.has(m));

  const scope: Scope = {
    source,
    date,
    agencyIds: by === "dopravce" ? agencyIds : [],
    modes: by === "dopravce" ? modes : [],
    routeId: by === "linka" ? search.linka : undefined,
    vehicle: by === "vozidlo" ? search.vozidlo : undefined,
  };
  const scopeBy = (next: By) => {
    setPicked(next);
    if (next !== by) {
      set({ dopravce: undefined, druh: undefined, linka: undefined, vozidlo: undefined });
    }
  };
  const circulations = useCirculations(scope, date === today);
  const rows = useMemo(
    () =>
      circulations.data
        ? rowsOf(circulations.data.vehicles, circulations.data.service_date, {
            vehicle: search.vuz,
            tripId: search.spoj,
          })
        : [],
    [circulations.data, search.vuz, search.spoj],
  );
  const span = spanOf(rows);
  const [selected, setSelected] = useState<string | null>(null);
  const complete = scopeComplete(scope);
  // Asking for another scope or day (not today's refresh): the old answer is dimmed meanwhile.
  const loading =
    complete && (circulations.isPlaceholderData || (circulations.isPending && !circulations.data));
  const company = chosen.map(([name]) => name).join(", ");

  return (
    <Panel label="Oběhy" wide>
      <div className={classes.page}>
        {(loading || scopes.isPlaceholderData) && (
          <div className={classes.progress} role="progressbar" aria-label="Načítání" />
        )}
        <header className={classes.head}>
          <h1 className={classes.title}>Oběhy</h1>
          <span className={classes.day}>{formatLongDay(`${date}T12:00:00Z`)}</span>
        </header>

        <div className={classes.filters}>
          <Group gap="xs" wrap="wrap">
            <Select
              aria-label="Zdroj"
              placeholder="Zdroj"
              data={sources}
              value={source ?? null}
              onChange={(v) => v && set({ zdroj: v })}
              allowDeselect={false}
              w={200}
            />
            <Group gap={4} wrap="nowrap">
              <ActionIcon
                variant="default"
                size="lg"
                aria-label="Předchozí den"
                onClick={() => set({ datum: addDays(date, -1) })}
              >
                <IconChevronLeft size={18} stroke={2} />
              </ActionIcon>
              <TextInput
                type="date"
                aria-label="Datum"
                value={date}
                max={today}
                onChange={(e) => e.currentTarget.value && set({ datum: e.currentTarget.value })}
                w={150}
              />
              <ActionIcon
                variant="default"
                size="lg"
                aria-label="Další den"
                disabled={date >= today}
                onClick={() => set({ datum: addDays(date, 1) })}
              >
                <IconChevronRight size={18} stroke={2} />
              </ActionIcon>
            </Group>
            <SegmentedControl
              value={by}
              onChange={(v) => scopeBy(v as By)}
              data={[
                { value: "dopravce", label: "Dopravce" },
                { value: "linka", label: "Linka" },
                ...(by === "vozidlo" ? [{ value: "vozidlo", label: "Vozidlo" }] : []),
              ]}
            />
          </Group>

          {by === "vozidlo" ? (
            <div className={classes.chosenLine}>
              <span className={classes.chosenVehicle}>
                {sourceName} {search.vozidlo}
              </span>
              <CloseButton
                className={classes.chosenClear}
                onClick={() => scopeBy("dopravce")}
                aria-label="Zrušit výběr vozidla"
              />
            </div>
          ) : by === "dopravce" ? (
            <>
              <MultiSelect
                aria-label="Dopravci"
                placeholder={chosen.length ? undefined : "Vyberte dopravce"}
                data={[...byName].map(([name, c]) => ({
                  value: name,
                  label: `${name} (${c.vehicles})`,
                }))}
                value={chosen.map(([name]) => name)}
                onChange={(names) =>
                  set({
                    dopravce: joined(names.flatMap((n) => byName.get(n)?.ids ?? [])),
                    linka: undefined,
                  })
                }
                searchable
                clearable
                nothingFoundMessage="Žádný dopravce"
                disabled={!settled}
              />
              {modesOffered.size > 1 && (
                <Chip.Group multiple value={modes} onChange={(v) => set({ druh: joined(v) })}>
                  <Group gap="xs">
                    {[...modesOffered].map(([m, n]) => (
                      <Chip key={m} value={m} size="sm">
                        {modesWord([m])} ({n})
                      </Chip>
                    ))}
                  </Group>
                </Chip.Group>
              )}
            </>
          ) : (
            <LinePicker
              routeId={search.linka}
              routes={scopes.data?.routes}
              onChange={(routeId) => set({ linka: routeId, dopravce: undefined, druh: undefined })}
            />
          )}

          <Group gap="sm" justify="space-between">
            {by === "vozidlo" ? (
              <span />
            ) : (
              <TextInput
                aria-label="Vozidlo"
                placeholder="Hledat vozidlo"
                leftSection={<IconSearch size={16} stroke={2} />}
                value={search.vuz ?? ""}
                onChange={(e) => set({ vuz: e.currentTarget.value || undefined })}
                w={220}
              />
            )}
            {complete && (circulations.data || loading) && (
              <span className={classes.count}>
                {loading ? "Načítám…" : vehiclesWord(rows.length)}
              </span>
            )}
          </Group>
        </div>

        {!complete ? (
          <BoardMessage
            title="Vyberte, co zobrazit"
            text="Zvolte zdroj a dopravce (případně druhy dopravy), nebo zdroj a linku."
          />
        ) : !circulations.data && loading ? (
          <div className={classes.loading} aria-busy="true" aria-label="Načítání">
            <p className={classes.loadingText}>
              Načítám oběhy{company ? ` – ${company}` : ""}… U velkých dopravců to chvíli trvá.
            </p>
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} height={22} />
            ))}
          </div>
        ) : circulations.isError && !circulations.data ? (
          <BoardMessage
            title="Oběhy se nepodařilo načíst"
            text="Zkontrolujte připojení. Zkusíme to znovu automaticky."
            action={{
              label: "Zkusit znovu",
              onClick: () => void circulations.refetch(),
              primary: true,
            }}
          />
        ) : !circulations.data ? (
          <div className={classes.loading} aria-busy="true" aria-label="Načítání">
            {[0, 1, 2, 3, 4].map((i) => (
              <Skeleton key={i} height={22} />
            ))}
          </div>
        ) : span === null ? (
          <BoardMessage
            title="Tento den žádné záznamy"
            text="Žádné vozidlo tohoto výběru jsme ten den na spoji neviděli."
          />
        ) : (
          <Timeline
            busy={loading}
            rows={rows}
            span={span}
            now={date === today ? secondsOfDay(date, now) : null}
            today={date === today}
            sourceLabel={sourceName}
            selected={selected}
            onSelect={setSelected}
          />
        )}
      </div>
    </Panel>
  );
}
