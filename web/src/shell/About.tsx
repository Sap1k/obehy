import { Modal } from "@mantine/core";
import {
  IconBrandGithub,
  IconDatabase,
  IconExternalLink,
  IconMail,
  type Icon,
} from "@tabler/icons-react";
import { Logo } from "../components/Logo";
import classes from "./About.module.css";

const REPOSITORY = "https://github.com/Sap1k/obehy";

/** The commit the app was built from (the web image's build passes it), empty in development. */
const COMMIT = import.meta.env.VITE_COMMIT ?? "";

const LINKS: { Icon: Icon; label: string; detail: string; href: string; external: boolean }[] = [
  {
    Icon: IconBrandGithub,
    label: "Zdrojový kód",
    detail: "github.com/Sap1k/obehy",
    href: REPOSITORY,
    external: true,
  },
  {
    Icon: IconDatabase,
    label: "Otevřená data",
    detail: "GTFS a GTFS-RT k volnému použití",
    href: "https://motis.obehy.cz/get-feeds/",
    external: true,
  },
  {
    Icon: IconMail,
    label: "Kontakt",
    detail: "admin@obehy.cz",
    href: "mailto:admin@obehy.cz",
    external: false,
  },
];

const TECH = [
  "Python",
  "FastAPI",
  "PostgreSQL",
  "PostGIS",
  "JrUtil (F#)",
  "MOTIS",
  "React",
  "Mantine",
  "MapLibre",
  "deck.gl",
  "OpenFreeMap",
  "OpenStreetMap",
];

/** What Oběhy is, where its code and data are, and the version running. */
export function About({ opened, onClose }: { opened: boolean; onClose: () => void }) {
  return (
    <Modal.Root
      opened={opened}
      onClose={onClose}
      centered
      size={440}
      radius="md"
      classNames={{ content: classes.content }}
    >
      <Modal.Overlay backgroundOpacity={0.35} blur={1} />
      <Modal.Content>
        <div className={`${classes.band} ob-dark-scope`}>
          <Logo height={40} className={classes.logo} />
          <Modal.CloseButton className={classes.close} aria-label="Zavřít" />
        </div>
        <div className={classes.body}>
          <Modal.Title className={classes.title}>O aplikaci</Modal.Title>
          <p className={classes.text}>
            Polohy vozidel, živé odjezdy a jízdní řády veřejné dopravy v celém Česku. Otevřený
            projekt, bez reklam.
          </p>
          <ul className={classes.links}>
            {LINKS.map(({ Icon, label, detail, href, external }) => (
              <li key={href}>
                <a
                  href={href}
                  className={classes.link}
                  {...(external ? { target: "_blank", rel: "noreferrer" } : {})}
                >
                  <span className={classes.icon} aria-hidden>
                    <Icon size={18} stroke={2} />
                  </span>
                  <span className={classes.linkText}>
                    <span className={classes.linkLabel}>{label}</span>
                    <span className={classes.linkDetail}>{detail}</span>
                  </span>
                  {external && (
                    <IconExternalLink
                      size={16}
                      stroke={2}
                      className={classes.external}
                      aria-label="(otevře jiný web)"
                    />
                  )}
                </a>
              </li>
            ))}
          </ul>
          <section aria-labelledby="about-tech">
            <h3 id="about-tech" className={classes.heading}>
              Postaveno na
            </h3>
            <ul className={classes.tech}>
              {TECH.map((name) => (
                <li key={name}>{name}</li>
              ))}
            </ul>
          </section>
          <p className={classes.version}>
            Verze{" "}
            {COMMIT ? (
              <a
                href={`${REPOSITORY}/commit/${COMMIT}`}
                target="_blank"
                rel="noreferrer"
                className={classes.commit}
              >
                {COMMIT.slice(0, 7)}
              </a>
            ) : (
              "vývojová"
            )}
          </p>
        </div>
      </Modal.Content>
    </Modal.Root>
  );
}
