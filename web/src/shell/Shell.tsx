import { ActionIcon, Modal, Tooltip, UnstyledButton } from "@mantine/core";
import { useNetwork } from "@mantine/hooks";
import {
  IconAlertTriangle,
  IconInfoCircle,
  IconLayoutSidebarLeftCollapse,
  IconLayoutSidebarLeftExpand,
  IconList,
  IconMap,
  IconRoute,
  IconSearch,
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
  { to: "/linky", label: "Linky", Icon: IconRoute, exact: false },
  { to: "/mimoradnosti", label: "Mimořádnosti", Icon: IconAlertTriangle, exact: false },
] as const;

const COLLAPSED_KEY = "obehy.nav.collapsed";

/** The map's paths: on the phone the map fills the main area there. */
function isMapPath(pathname: string): boolean {
  return pathname === "/" || pathname.startsWith("/vozidlo/");
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
      </nav>

      <Modal opened={searching} onClose={() => setSearching(false)} title="Hledat" fullScreen>
        <Search autoFocus onPicked={() => setSearching(false)} />
      </Modal>
      <About opened={about} onClose={() => setAbout(false)} />
      <DukfinderWelcome />
    </div>
  );
}
