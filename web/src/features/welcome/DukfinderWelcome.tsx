import { Button, Modal } from "@mantine/core";
import { IconClock, IconList, IconMap2 } from "@tabler/icons-react";
import { Logo } from "../../components/Logo";
import { referral } from "../../lib/referral";
import { usePersistentFlag } from "../../lib/storage";
import classes from "./DukfinderWelcome.module.css";

const SEEN_KEY = "obehy.dukfinder.welcomed";

// What Oběhy does better (DÚKfinder had trains too, with delays one could not rely on).
const WHAT = [
  { Icon: IconClock, text: "Přesnější zpoždění u vlaků i autobusů" },
  { Icon: IconMap2, text: "Celé Česko: autobusy, vlaky, tramvaje i metro" },
  { Icon: IconList, text: "Živé odjezdy ze zastávek a jízdní řády" },
] as const;

/** Readers of the old DÚKfinder, whose address now redirects here: a welcome saying it lives on
 * as Oběhy, over a lightly dimmed map. Once per browser. */
export function DukfinderWelcome() {
  const [seen, setSeen] = usePersistentFlag(SEEN_KEY, "local");
  if (referral !== "dukfinder" || seen) return null;
  const close = () => setSeen(true);
  return (
    <Modal.Root
      opened
      onClose={close}
      centered
      size={440}
      radius="md"
      // Only the dialog: `className` on Modal.Content would also reach the full-screen wrapper
      // around it and paint over the map.
      classNames={{ content: classes.content }}
    >
      {/* Light, so the live map shows behind it. */}
      <Modal.Overlay backgroundOpacity={0.25} blur={1} />
      <Modal.Content>
        <div className={`${classes.band} ob-dark-scope`}>
          <Logo height={44} className={classes.logo} />
        </div>
        <div className={classes.body}>
          <Modal.Title className={classes.title}>DÚKfinder má nový domov</Modal.Title>
          <p className={classes.text}>
            DÚKfinder skončil a jeho místo zaujaly Oběhy: jeho nástupce, který umí všechno, co
            znáte, a mnohem víc.
          </p>
          <ul className={classes.list}>
            {WHAT.map(({ Icon, text }) => (
              <li key={text}>
                <span className={classes.icon} aria-hidden>
                  <Icon size={18} stroke={2} />
                </span>
                {text}
              </li>
            ))}
          </ul>
          <p className={classes.text}>
            Díky, že jste jezdili s DÚKfinderem. Uložte si <strong>obehy.cz</strong> a šťastnou
            cestu!
          </p>
          <Button fullWidth size="md" onClick={close} data-autofocus>
            Pojďme na to
          </Button>
        </div>
      </Modal.Content>
    </Modal.Root>
  );
}
