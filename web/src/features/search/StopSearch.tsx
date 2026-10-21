import { Combobox, Loader, TextInput, useCombobox } from "@mantine/core";
import { useDebouncedValue } from "@mantine/hooks";
import { IconSearch } from "@tabler/icons-react";
import { useQuery } from "@tanstack/react-query";
import { useNavigate } from "@tanstack/react-router";
import { useEffect, useMemo, useState } from "react";
import { api, unwrap } from "../../api/client";
import classes from "./StopSearch.module.css";

/** The API's shortest name search. */
const MIN_CHARS = 2;

/** Stop search (Figma "Search"): debounced, by name and municipality; Enter opens the first. */
export function StopSearch({
  autoFocus = false,
  onPicked,
}: {
  autoFocus?: boolean;
  onPicked?: () => void;
}) {
  const navigate = useNavigate();
  const combobox = useCombobox({ onDropdownClose: () => combobox.resetSelectedOption() });
  const [text, setText] = useState("");
  const [q] = useDebouncedValue(text.trim(), 200);
  const enabled = q.length >= MIN_CHARS;
  const results = useQuery({
    queryKey: ["stops", q],
    queryFn: async ({ signal }) =>
      unwrap(await api.GET("/api/v1/stops", { params: { query: { q, limit: 10 } }, signal })),
    enabled,
    staleTime: 5 * 60_000,
  });
  const stops = useMemo(
    () => (enabled ? (results.data?.stops ?? []) : []),
    [enabled, results.data],
  );

  useEffect(() => {
    if (stops.length > 0) combobox.selectFirstOption();
  }, [stops, combobox]);

  const open = (locationId: string) => {
    combobox.closeDropdown();
    setText("");
    onPicked?.();
    void navigate({ to: "/odjezdy/$stopId", params: { stopId: locationId } });
  };

  return (
    <Combobox store={combobox} onOptionSubmit={open} withinPortal={false}>
      <Combobox.Target>
        <TextInput
          className={classes.input}
          size="md"
          autoFocus={autoFocus}
          value={text}
          placeholder="Hledat zastávku"
          aria-label="Hledat zastávku"
          leftSection={<IconSearch size={20} stroke={2} />}
          rightSection={results.isFetching && enabled ? <Loader size={16} /> : null}
          onChange={(e) => {
            setText(e.currentTarget.value);
            combobox.openDropdown();
          }}
          onFocus={() => combobox.openDropdown()}
          onBlur={() => combobox.closeDropdown()}
        />
      </Combobox.Target>
      <Combobox.Dropdown hidden={!enabled}>
        <Combobox.Options>
          {stops.map((s) => (
            <Combobox.Option value={s.location_id} key={s.location_id} className={classes.option}>
              <span className={classes.name}>{s.name}</span>
              {s.municipality && s.municipality !== s.name && (
                <span className={classes.place}>{s.municipality}</span>
              )}
            </Combobox.Option>
          ))}
          {enabled && results.isSuccess && stops.length === 0 && (
            <Combobox.Empty>Nic jsme nenašli</Combobox.Empty>
          )}
          {enabled && results.isError && <Combobox.Empty>Hledání se nepodařilo</Combobox.Empty>}
        </Combobox.Options>
      </Combobox.Dropdown>
    </Combobox>
  );
}
