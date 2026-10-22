import { ActionIcon, Modal } from "@mantine/core";
import { IconAlertTriangle, IconList, IconMap, IconRoute, IconSearch } from "@tabler/icons-react";
import { Link, Outlet, useRouterState } from "@tanstack/react-router";
import { useState } from "react";
import { DataStatus } from "../components/DataStatus";
import { Logo } from "../components/Logo";
import { StopSearch } from "../features/search/StopSearch";
import { useReportedFreshness } from "../lib/freshness";
import classes from "./Shell.module.css";

const TABS = [
  { to: "/", label: "Mapa", Icon: IconMap, exact: true },
  { to: "/odjezdy", label: "Odjezdy", Icon: IconList, exact: false },
  { to: "/linky", label: "Linky", Icon: IconRoute, exact: false },
  { to: "/mimoradnosti", label: "Mimořádnosti", Icon: IconAlertTriangle, exact: false },
] as const;

/** The map's paths: it fills the main area, with the search floating over it. */
function isMapPath(pathname: string): boolean {
  return pathname === "/" || pathname.startsWith("/vozidlo/");
}

/** The app shell (Figma 11, 13): a dark navbar with the tabs on the web; on the phone a top bar
 * with the data status and search, and a bottom tab bar. */
export function Shell() {
  const freshness = useReportedFreshness();
  const [searching, setSearching] = useState(false);
  const onMap = isMapPath(useRouterState({ select: (s) => s.location.pathname }));
  // The Mapa tab is active on a vehicle too (its path is not under "/" for exact matching).
  const tabProps = (to: string) =>
    to === "/" && onMap ? { "data-active": true as const } : undefined;
  return (
    <div className={classes.shell}>
      <nav className={`${classes.navbar} ob-dark-scope`} aria-label="Hlavní">
        <Link to="/" className={classes.logo} aria-label="Oběhy">
          <Logo height={40} />
        </Link>
        <ul className={classes.tabs}>
          {TABS.map(({ to, label, Icon, exact }) => (
            <li key={to}>
              <Link
                to={to}
                className={classes.tab}
                activeOptions={{ exact }}
                activeProps={{ "data-active": true }}
                {...tabProps(to)}
              >
                <Icon size={20} stroke={2} />
                {label}
              </Link>
            </li>
          ))}
        </ul>
        <div className={classes.foot}>Oběhy · electric</div>
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
            onClick={() => setSearching(true)}
            aria-label="Hledat"
          >
            <IconSearch size={22} stroke={2} />
          </ActionIcon>
        </span>
      </header>

      <main className={classes.main} data-bleed={onMap || undefined}>
        <div className={classes.toolbar}>
          <StopSearch />
          {freshness && <DataStatus freshness={freshness} />}
        </div>
        <Outlet />
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

      <Modal
        opened={searching}
        onClose={() => setSearching(false)}
        title="Hledat zastávku"
        fullScreen
      >
        <StopSearch autoFocus onPicked={() => setSearching(false)} />
      </Modal>
    </div>
  );
}
