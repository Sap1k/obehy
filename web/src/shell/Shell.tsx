import { ActionIcon, Drawer, Menu, Modal, Tooltip, UnstyledButton } from "@mantine/core";
import { useNetwork } from "@mantine/hooks";
import {
  IconAlertTriangle,
  IconDirections,
  IconDots,
  IconExternalLink,
  IconInfoCircle,
  IconLayoutSidebarLeftCollapse,
  IconLayoutSidebarLeftExpand,
  IconList,
  IconMap,
  IconRoute,
  IconSearch,
  IconTimeline,
  type Icon,
} from "@tabler/icons-react";
import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { Suspense, lazy, useEffect, useRef, useState } from "react";
import { DataStatus } from "../components/DataStatus";
import { Logo } from "../components/Logo";
import { setPanelsRight } from "../features/map/store";
import { DukfinderWelcome } from "../features/welcome/DukfinderWelcome";
import { Search } from "../features/search/Search";
import { useQuery } from "@tanstack/react-query";
import { api, unwrap } from "../api/client";
import { freshnessOf, useStatusQueriesFailing } from "../lib/freshness";
import { useNow } from "../lib/hooks";
import { usePhone } from "../lib/layout";
import { usePersistentFlag } from "../lib/storage";
import { About } from "./About";
import classes from "./Shell.module.css";

// The map under the app, loaded with its own chunk (MapLibre and deck.gl).
const MapStage = lazy(() =>
  import("../features/map/MapPage").then((m) => ({ default: m.MapStage })),
);

const TABS = [
  { to: "/", label: "Mapa", Icon: IconMap, exact: true },
  { to: "/odjezdy", label: "Odjezdy", Icon: IconList, exact: false },
] as const;

/** The journey planner, an app of its own. */
const MOTIS = { href: "https://motis.obehy.cz/", label: "MOTIS", Icon: IconDirections };

/** The rest of the app, under "Další". */
const MORE: readonly { to: string; label: string; Icon: Icon }[] = [
  { to: "/linky", label: "Linky", Icon: IconRoute },
  { to: "/obehy", label: "Oběhy", Icon: IconTimeline },
  { to: "/mimoradnosti", label: "Mimořádnosti", Icon: IconAlertTriangle },
];

const COLLAPSED_KEY = "obehy.nav.collapsed";

/** The map's paths: on the phone the map fills the main area there. */
function isMapPath(pathname: string): boolean {
  return pathname === "/" || pathname.startsWith("/vozidlo/");
}

/** Whether one of "Další"'s pages is shown. */
function useMoreActive() {
  const pathname = useRouterState({ select: (s) => s.location.pathname });
  return MORE.some(({ to }) => pathname === to || pathname.startsWith(`${to}/`));
}

/** "Další" in the navbar: a menu of the rest of the app. */
function MoreMenu({ collapsed }: { collapsed: boolean }) {
  const active = useMoreActive();
  return (
    <Menu position="right-start" offset={12} width={220} withinPortal>
      <Menu.Target>
        <UnstyledButton
          className={classes.tab}
          data-active={active || undefined}
          aria-label={collapsed ? "Další" : undefined}
        >
          <IconDots size={20} stroke={2} />
          {!collapsed && "Další"}
        </UnstyledButton>
      </Menu.Target>
      <Menu.Dropdown>
        {MORE.map(({ to, label, Icon }) => (
          <Menu.Item
            key={to}
            component={Link}
            to={to}
            className={classes.moreItem}
            leftSection={<Icon size={20} stroke={2} />}
          >
            {label}
          </Menu.Item>
        ))}
      </Menu.Dropdown>
    </Menu>
  );
}

/** "Další" in the phone's tab bar: a sheet from the bottom with the rest of the app. */
function MoreSheet() {
  const active = useMoreActive();
  const [opened, setOpened] = useState(false);
  return (
    <>
      <UnstyledButton
        className={classes.bottomTab}
        data-active={active || undefined}
        onClick={() => setOpened(true)}
      >
        <IconDots size={22} stroke={2} />
        Další
      </UnstyledButton>
      <Drawer
        opened={opened}
        onClose={() => setOpened(false)}
        position="bottom"
        size="auto"
        title="Další"
        classNames={{
          content: classes.sheet,
          header: classes.sheetHeader,
          body: classes.sheetBody,
        }}
      >
        <nav aria-label="Další">
          {MORE.map(({ to, label, Icon }) => (
            <Link
              key={to}
              to={to}
              className={classes.sheetItem}
              activeProps={{ "data-active": true }}
              onClick={() => setOpened(false)}
            >
              <Icon size={22} stroke={2} />
              {label}
            </Link>
          ))}
        </nav>
      </Drawer>
    </>
  );
}

/** The data status: how recently any source's vehicle report came in (`/status`, polled), and
 * whether the page's requests fail. Nothing until the first answer. */
function useFreshness() {
  const status = useQuery({
    queryKey: ["status"],
    queryFn: async ({ signal }) => unwrap(await api.GET("/api/v1/status", { signal })),
    refetchInterval: 30_000,
    staleTime: 20_000,
  });
  const pageFailing = useStatusQueriesFailing();
  const network = useNetwork();
  const now = useNow(5000);
  if (!status.data && !status.isError) return null;
  // An API from before `realtime_at` cannot say how live the data is: only failures show.
  const failing = pageFailing || status.isError || !network.online;
  if (status.data && status.data.realtime_at === undefined) return failing ? "offline" : null;
  return freshnessOf({
    failing: pageFailing || status.isError,
    online: network.online,
    realtimeAt: status.data?.realtime_at,
    now,
  });
}

/** The app shell (Figma 11, 13). The web: a dark navbar with the tabs (collapsible to icons),
 * and the map under everything, the tab's panels over it on the left. The phone: a top bar
 * with the data status and search, the tab as a page, and a bottom tab bar. */
export function Shell() {
  const freshness = useFreshness();
  const [searching, setSearching] = useState(false);
  const [about, setAbout] = useState(false);
  const [collapsed, setCollapsed] = usePersistentFlag(COLLAPSED_KEY, "local");
  const phone = usePhone();
  const onMap = isMapPath(useRouterState({ select: (s) => s.location.pathname }));
  // The Mapa tab is active on a vehicle too (its path is not under "/" for exact matching).
  const tabProps = (to: string) =>
    to === "/" && onMap ? { "data-active": true as const } : undefined;

  // The map leaves the panels out when it centres: tell it how far they reach.
  const panels = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const el = panels.current;
    if (!el || phone) {
      setPanelsRight(0);
      return;
    }
    const measure = () => setPanelsRight(el.offsetWidth > 0 ? el.offsetLeft + el.offsetWidth : 0);
    const observer = new ResizeObserver(measure);
    observer.observe(el);
    measure();
    return () => observer.disconnect();
  }, [phone]);

  return (
    <div className={classes.shell} data-collapsed={collapsed || undefined}>
      <nav className={`${classes.navbar} ob-dark-scope`} aria-label="Hlavní">
        <Link to="/" className={classes.logo} aria-label="Oběhy">
          {!collapsed && <Logo height={40} />}
        </Link>
        <ul className={classes.tabs}>
          {TABS.map(({ to, label, Icon, exact }) => (
            <li key={to}>
              <Tooltip label={label} position="right" disabled={!collapsed} withArrow>
                <Link
                  to={to}
                  className={classes.tab}
                  activeOptions={{ exact }}
                  activeProps={{ "data-active": true }}
                  aria-label={collapsed ? label : undefined}
                  {...tabProps(to)}
                >
                  <Icon size={20} stroke={2} />
                  {!collapsed && label}
                </Link>
              </Tooltip>
            </li>
          ))}
          <li>
            <Tooltip label={MOTIS.label} position="right" disabled={!collapsed} withArrow>
              <a
                href={MOTIS.href}
                target="_blank"
                rel="noreferrer"
                className={classes.tab}
                aria-label={collapsed ? `${MOTIS.label} (nové okno)` : undefined}
              >
                <MOTIS.Icon size={20} stroke={2} />
                {!collapsed && (
                  <>
                    {MOTIS.label}
                    <IconExternalLink size={14} stroke={2} aria-label="nové okno" />
                  </>
                )}
              </a>
            </Tooltip>
          </li>
          <li>
            <MoreMenu collapsed={collapsed} />
          </li>
        </ul>
        <div className={classes.foot}>
          <Tooltip label="O aplikaci" position="right" disabled={!collapsed} withArrow>
            <UnstyledButton
              className={classes.about}
              onClick={() => setAbout(true)}
              aria-label={collapsed ? "O aplikaci" : undefined}
            >
              <IconInfoCircle size={18} stroke={2} />
              {!collapsed && "O aplikaci"}
            </UnstyledButton>
          </Tooltip>
          <ActionIcon
            variant="subtle"
            color="gray"
            onClick={() => setCollapsed(!collapsed)}
            aria-label={collapsed ? "Rozbalit nabídku" : "Sbalit nabídku"}
          >
            {collapsed ? (
              <IconLayoutSidebarLeftExpand size={20} stroke={2} />
            ) : (
              <IconLayoutSidebarLeftCollapse size={20} stroke={2} />
            )}
          </ActionIcon>
        </div>
      </nav>

      <header className={classes.topbar}>
        <Link to="/" className={classes.logo} aria-label="Oběhy">
          <Logo height={32} />
        </Link>
        <span className={classes.topbarEnd}>
          {freshness && <DataStatus freshness={freshness} />}
          <ActionIcon
            variant="subtle"
            color="gray"
            size="lg"
            onClick={() => setAbout(true)}
            aria-label="O aplikaci"
          >
            <IconInfoCircle size={22} stroke={2} />
          </ActionIcon>
          <ActionIcon
            variant="subtle"
            color="gray"
            size="lg"
            onClick={() => setSearching(true)}
            aria-label="Hledat"
          >
            <IconSearch size={22} stroke={2} />
          </ActionIcon>
        </span>
      </header>

      <main className={classes.main} data-bleed={onMap || undefined}>
        {/* One map for the app: on the phone shown on the map tab only (kept, idle, elsewhere). */}
        <div className={classes.stage} data-hidden={(phone && !onMap) || undefined}>
          <Suspense fallback={null}>
            <MapStage hidden={phone && !onMap} />
          </Suspense>
        </div>
        <div className={classes.toolbar}>
          <Search />
          {freshness && <DataStatus freshness={freshness} />}
        </div>
        <div ref={panels} className={classes.panels}>
          <Outlet />
        </div>
      </main>

      <nav className={classes.bottom} aria-label="Hlavní">
        {TABS.map(({ to, label, Icon, exact }) => (
          <Link
            key={to}
            to={to}
            className={classes.bottomTab}
            activeOptions={{ exact }}
            activeProps={{ "data-active": true }}
            {...tabProps(to)}
          >
            <Icon size={22} stroke={2} />
            {label}
          </Link>
        ))}
        <a href={MOTIS.href} target="_blank" rel="noreferrer" className={classes.bottomTab}>
          <MOTIS.Icon size={22} stroke={2} />
          {MOTIS.label}
        </a>
        <MoreSheet />
      </nav>

      <Modal opened={searching} onClose={() => setSearching(false)} title="Hledat" fullScreen>
        <Search autoFocus onPicked={() => setSearching(false)} />
      </Modal>
      <About opened={about} onClose={() => setAbout(false)} />
      <DukfinderWelcome />
    </div>
  );
}
