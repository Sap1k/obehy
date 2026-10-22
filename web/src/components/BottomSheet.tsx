import { useEffect, useLayoutEffect, useRef, useState, type ReactNode } from "react";
import classes from "./BottomSheet.module.css";

// A sheet over the map on the phone: peek (the header), half, or full. Non-modal, so the map
// stays usable above it. Dragged by its handle; a tap on the handle steps it up (and from full
// back to peek).

export type SheetSnap = "peek" | "half" | "full";

const PEEK_PX = 168;
const HANDLE_PX = 28;
const TOP_GAP_PX = 16;
/** A flick faster than this (px/ms) goes on to the next snap in its direction. */
const FLICK = 0.5;
const ORDER: SheetSnap[] = ["peek", "half", "full"];

export function BottomSheet({
  snap,
  onSnap,
  onHeight,
  children,
}: {
  snap: SheetSnap;
  onSnap: (snap: SheetSnap) => void;
  /** The visible height once settled, for the map's padding. */
  onHeight: (px: number) => void;
  children: ReactNode;
}) {
  const host = useRef<HTMLDivElement>(null);
  const sheet = useRef<HTMLDivElement>(null);
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

  const full = Math.max(PEEK_PX, hostHeight - TOP_GAP_PX);
  const heights: Record<SheetSnap, number> = {
    peek: Math.min(PEEK_PX, full),
    half: Math.max(PEEK_PX, Math.round(hostHeight / 2)),
    full,
  };
  const visible = heights[snap];

  useEffect(() => {
    if (hostHeight > 0) onHeight(visible);
  }, [visible, hostHeight, onHeight]);

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
    const at = ORDER.indexOf(snap);
    if (!d.moved) {
      onSnap(ORDER[(at + 1) % ORDER.length]!);
      return;
    }
    const dy = e.clientY - d.y;
    const now = d.visible - dy;
    const speed = -dy / Math.max(1, e.timeStamp - d.t);
    let next: SheetSnap;
    if (Math.abs(speed) > FLICK) {
      next = ORDER[Math.min(ORDER.length - 1, Math.max(0, at + (speed > 0 ? 1 : -1)))]!;
    } else {
      next = ORDER.reduce((best, s) =>
        Math.abs(heights[s] - now) < Math.abs(heights[best] - now) ? s : best,
      );
    }
    if (next === snap) place(visible, true);
    else onSnap(next);
  };

  return (
    <div ref={host} className={classes.host}>
      <div ref={sheet} className={classes.sheet} style={{ height: full }}>
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
