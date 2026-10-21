import { IconAlertTriangle, IconChevronRight, IconMap, IconRoute } from "@tabler/icons-react";
import { Link } from "@tanstack/react-router";
import { useState, type ComponentType } from "react";
import { StopSearch } from "../features/search/StopSearch";
import { readRecent } from "../lib/recent";
import classes from "./pages.module.css";

function Placeholder({
  title,
  text,
  Icon,
  withSearch = false,
}: {
  title: string;
  text: string;
  Icon: ComponentType<{ size?: number; stroke?: number }>;
  withSearch?: boolean;
}) {
  return (
    <section className={classes.placeholder}>
      <span className={classes.icon} aria-hidden>
        <Icon size={28} stroke={2} />
      </span>
      <h1 className={classes.title}>{title}</h1>
      <p className={classes.text}>{text}</p>
      {withSearch && (
        <div className={classes.search}>
          <StopSearch />
        </div>
      )}
      <RecentStops />
    </section>
  );
}

function RecentStops() {
  const [recent] = useState(readRecent);
  if (recent.length === 0) return null;
  return (
    <div className={classes.recent}>
      <h2 className={classes.recentTitle}>Naposledy otevřené</h2>
      <ul className={classes.recentList}>
        {recent.map((s) => (
          <li key={s.location_id}>
            <Link
              to="/odjezdy/$stopId"
              params={{ stopId: s.location_id }}
              className={classes.recentItem}
            >
              <span>
                <span className={classes.recentName}>{s.name}</span>
                {s.municipality && s.municipality !== s.name && (
                  <span className={classes.recentPlace}>{s.municipality}</span>
                )}
              </span>
              <IconChevronRight size={20} stroke={2} aria-hidden />
            </Link>
          </li>
        ))}
      </ul>
    </div>
  );
}

export function MapPage() {
  return (
    <Placeholder
      title="Mapa"
      text="Mapa zastávek, linek a vozidel se připravuje. Zatím najděte zastávku a podívejte se na její odjezdy."
      Icon={IconMap}
      withSearch
    />
  );
}

export function DeparturesIndex() {
  return (
    <Placeholder
      title="Odjezdy"
      text="Vyhledejte zastávku a uvidíte odjezdy a příjezdy v reálném čase."
      Icon={IconRoute}
      withSearch
    />
  );
}

export function LinesPage() {
  return (
    <Placeholder
      title="Linky"
      text="Přehled linek a jejich jízdních řádů se připravuje."
      Icon={IconRoute}
    />
  );
}

export function AlertsPage() {
  return (
    <Placeholder
      title="Mimořádnosti"
      text="Přehled výluk a mimořádností se připravuje. Ty, které se týkají zastávky, ukazují odjezdy."
      Icon={IconAlertTriangle}
    />
  );
}
