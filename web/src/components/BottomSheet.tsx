import { useLayoutEffect, useRef, useState, type ReactNode } from "react";
import { setSnap, useMapStore } from "../features/map/store";
import { useGhostExit } from "../lib/ghost";
import { SNAPS, sheetHeights, type SheetSnap } from "../lib/sheet";
import classes from "./BottomSheet.module.css";

// A sheet over the map on the phone, at the map store's snap (lib/sheet.ts; the map keeps what it
// centres clear of the same heights). Non-modal, so the map stays usable. Dragged by its handle;
// a tap on the handle steps it up (and from full back to peek). Its content scrolls inside.

const HANDLE_PX = 28;
/** A flick faster than this (px/ms) goes on to the next snap in its direction. */
const FLICK = 0.5;
const SLIDE_DOWN: Keyframe[] = [{}, { transform: "translateY(100vh)" }];

export function BottomSheet({ children }: { children: ReactNode }) {
  const snap = useMapStore((s) => s.snap);
  const host = useRef<HTMLDivElement>(null);
  // Closing: a copy slides down off the screen (React removes the sheet at once).
  const sheet = useRef<HTMLDivElement>(null);
  useGhostExit(sheet, SLIDE_DOWN, 220);
  const [hostHeight, setHostHeight] = useState(0);
  const drag = useRef<{ y: number; visible: number; t: number; moved: boolean } | null>(null);

  useLayoutEffect(() => {
    const el = host.current;
    if (!el) return;
    const observer = new ResizeObserver(() => setHostHeight(el.clientHeight));
    observer.observe(el);
    setHostHeight(el.clientHeight);
    return () => observer.disconnect();
  }, []);

  const heights = sheetHeights(hostHeight);
  const full = heights.full;
  const visible = heights[snap];

  // Dragging moves the sheet directly; the snap's place is set after every render.
  const place = (px: number, animate: boolean) => {
    const el = sheet.current;
    if (!el) return;
    el.style.transition = animate ? "" : "none";
    el.style.transform = `translateY(${full - px}px)`;
  };
  useLayoutEffect(() => place(visible, true));

  const onPointerDown = (e: React.PointerEvent) => {
    e.currentTarget.setPointerCapture(e.pointerId);
    drag.current = { y: e.clientY, visible, t: e.timeStamp, moved: false };
  };
  const onPointerMove = (e: React.PointerEvent) => {
    const d = drag.current;
    if (!d) return;
    const dy = e.clientY - d.y;
    if (Math.abs(dy) > 4) d.moved = true;
    place(Math.min(full, Math.max(heights.peek, d.visible - dy)), false);
  };
  const onPointerUp = (e: React.PointerEvent) => {
    const d = drag.current;
    drag.current = null;
    if (!d) return;
    const at = SNAPS.indexOf(snap);
    if (!d.moved) {
      setSnap(SNAPS[(at + 1) % SNAPS.length]!);
      return;
    }
    const dy = e.clientY - d.y;
    const now = d.visible - dy;
    const speed = -dy / Math.max(1, e.timeStamp - d.t);
    let next: SheetSnap;
    if (Math.abs(speed) > FLICK) {
      next = SNAPS[Math.min(SNAPS.length - 1, Math.max(0, at + (speed > 0 ? 1 : -1)))]!;
    } else {
      next = SNAPS.reduce((best, s) =>
        Math.abs(heights[s] - now) < Math.abs(heights[best] - now) ? s : best,
      );
    }
    if (next === snap) place(visible, true);
    else setSnap(next);
  };

  return (
    <div ref={host} className={classes.host}>
      <div ref={sheet} className={`${classes.sheet} ${classes.entering}`} style={{ height: full }}>
        <div
          className={classes.handle}
          style={{ height: HANDLE_PX }}
          onPointerDown={onPointerDown}
          onPointerMove={onPointerMove}
          onPointerUp={onPointerUp}
          onPointerCancel={onPointerUp}
          role="button"
          aria-label="Změnit velikost panelu"
        >
          <span className={classes.grip} />
        </div>
        <div className={classes.content} style={{ height: visible - HANDLE_PX }}>
          {children}
        </div>
      </div>
    </div>
  );
}
