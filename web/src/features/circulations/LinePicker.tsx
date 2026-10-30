import { ActionIcon, Combobox, Loader, TextInput, useCombobox } from "@mantine/core";
import { IconSearch, IconX } from "@tabler/icons-react";
import { useEffect, useMemo, useState } from "react";
import type { ScopeRoute } from "../../api/client";
import { LineBadge, lineLabel } from "../../components/LineBadge";
import { linesMatching } from "./layout";
import searchClasses from "../search/Search.module.css";
import classes from "./CirculationsPage.module.css";

// The oběhy page's line: one of the lines the source's vehicles worked that day, searched as
// the app's search finds things (a badge, the line's name, its operator; Enter takes the first),
// shown once chosen as its badge and name.

/** Matches shown at once; typing narrows them. */
const SHOWN = 20;

export function LinePicker({
  routeId,
  routes,
  onChange,
}: {
  routeId: string | undefined;
  /** The day's lines; undefined while they load. */
  routes: ScopeRoute[] | undefined;
  onChange: (routeId: string | undefined) => void;
}) {
  const combobox = useCombobox({ onDropdownClose: () => combobox.resetSelectedOption() });
  const [text, setText] = useState("");
  const q = text.trim();
  const lines = useMemo(
    () => (q && routes ? linesMatching(routes, q).slice(0, SHOWN) : []),
    [q, routes],
  );

  useEffect(() => {
    if (lines.length > 0) combobox.selectFirstOption();
  }, [lines, combobox]);

  if (routeId) {
    const chosen = routes?.find((r) => r.line.route_id === routeId);
    return (
      <div className={classes.chosenLine}>
        {chosen ? (
          <>
            <LineBadge line={chosen.line} />
            <span className={searchClasses.texts}>
              <span className={searchClasses.name}>
                {chosen.line.long_name ?? lineLabel(chosen.line)}
              </span>
              {chosen.agency && <span className={searchClasses.place}>{chosen.agency}</span>}
            </span>
          </>
        ) : routes ? (
          <span className={searchClasses.texts}>
            <span className={searchClasses.name}>Linka tento den bez záznamů</span>
          </span>
        ) : (
          <Loader size={16} />
        )}
        <ActionIcon
          variant="subtle"
          color="gray"
          className={classes.chosenClear}
          onClick={() => onChange(undefined)}
          aria-label="Vybrat jinou linku"
        >
          <IconX size={18} stroke={2} />
        </ActionIcon>
      </div>
    );
  }

  const pick = (value: string) => {
    combobox.closeDropdown();
    setText("");
    onChange(value);
  };

  return (
    <Combobox store={combobox} onOptionSubmit={pick} withinPortal={false}>
      <Combobox.Target>
        <TextInput
          className={searchClasses.input}
          value={text}
          placeholder="Číslo nebo název linky"
          aria-label="Linka"
          leftSection={<IconSearch size={18} stroke={2} />}
          rightSection={q && !routes ? <Loader size={16} /> : null}
          onChange={(e) => {
            setText(e.currentTarget.value);
            combobox.openDropdown();
          }}
          onFocus={() => combobox.openDropdown()}
          onBlur={() => combobox.closeDropdown()}
        />
      </Combobox.Target>
      <Combobox.Dropdown hidden={!q || !routes}>
        <Combobox.Options className={searchClasses.options}>
          {lines.map((l) => (
            <Combobox.Option
              key={l.line.route_id}
              value={l.line.route_id}
              className={searchClasses.option}
            >
              <LineBadge line={l.line} />
              <span className={searchClasses.texts}>
                <span className={searchClasses.name}>{l.line.long_name ?? lineLabel(l.line)}</span>
                {l.agency && <span className={searchClasses.place}>{l.agency}</span>}
              </span>
            </Combobox.Option>
          ))}
          {lines.length === 0 && <Combobox.Empty>Tento den žádná taková linka</Combobox.Empty>}
        </Combobox.Options>
      </Combobox.Dropdown>
    </Combobox>
  );
}
