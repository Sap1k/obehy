import { Button, Group, Popover, SegmentedControl, Stack, TextInput } from "@mantine/core";
import { IconChevronDown, IconClock } from "@tabler/icons-react";
import { useState } from "react";
import { addDays, dayKey, formatClock, pragueInstant, relativeDay } from "../lib/time";
import classes from "./TimeSelect.module.css";

type Day = "today" | "tomorrow" | "other";

/** "Teď" or "Dnes, 14:30": the board's start, chosen as a day and a time (Figma "Time select"). */
export function timeLabel(value: string | undefined, now: number): string {
  if (!value) return "Teď";
  return `${relativeDay(value, now)}, ${formatClock(value)}`;
}

export function TimeSelect({
  value,
  onChange,
  now,
  disabled = false,
}: {
  value: string | undefined;
  now: number;
  onChange: (value: string | undefined) => void;
  disabled?: boolean;
}) {
  const [opened, setOpened] = useState(false);
  const [day, setDay] = useState<Day>("today");
  const [date, setDate] = useState("");
  const [time, setTime] = useState("");

  const open = () => {
    const now = Date.now();
    const start = value ?? new Date(now).toISOString();
    const today = dayKey(now);
    const key = dayKey(start);
    setDay(key === today ? "today" : key === addDays(today, 1) ? "tomorrow" : "other");
    setDate(key);
    setTime(formatClock(start));
    setOpened(true);
  };

  const apply = () => {
    const today = dayKey(Date.now());
    const chosen = day === "today" ? today : day === "tomorrow" ? addDays(today, 1) : date;
    if (!chosen || !/^\d{1,2}:\d{2}$/.test(time)) return;
    onChange(pragueInstant(chosen, time).toISOString());
    setOpened(false);
  };

  return (
    <Popover opened={opened} onChange={setOpened} position="bottom-start" width={280}>
      <Popover.Target>
        <button
          type="button"
          className={`${classes.target} ${value ? classes.changed : ""}`}
          onClick={() => (opened ? setOpened(false) : open())}
          disabled={disabled}
          aria-haspopup="dialog"
          aria-expanded={opened}
        >
          <IconClock size={20} stroke={2} />
          <span className={classes.label}>{timeLabel(value, now)}</span>
          <IconChevronDown size={20} stroke={2} className={classes.chevron} />
        </button>
      </Popover.Target>
      <Popover.Dropdown>
        <Stack gap="md">
          <SegmentedControl
            fullWidth
            value={day}
            onChange={(v) => setDay(v as Day)}
            data={[
              { value: "today", label: "Dnes" },
              { value: "tomorrow", label: "Zítra" },
              { value: "other", label: "Jiný den" },
            ]}
          />
          {day === "other" && (
            <TextInput
              type="date"
              label="Den"
              value={date}
              onChange={(e) => setDate(e.currentTarget.value)}
            />
          )}
          <TextInput
            type="time"
            label="Čas"
            value={time}
            onChange={(e) => setTime(e.currentTarget.value)}
            onKeyDown={(e) => e.key === "Enter" && apply()}
          />
          <Group justify="space-between">
            <Button
              variant="subtle"
              onClick={() => {
                onChange(undefined);
                setOpened(false);
              }}
            >
              Teď
            </Button>
            <Button onClick={apply}>Zobrazit</Button>
          </Group>
        </Stack>
      </Popover.Dropdown>
    </Popover>
  );
}
