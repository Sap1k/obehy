// Keeping an element where it was on screen while content above it opens or closes: the
// nearest scrolling ancestor (a panel, the phone's sheet) or the page scrolls by as much as the
// element moved.

function scrollingAncestor(el: HTMLElement): HTMLElement | null {
  for (let p = el.parentElement; p; p = p.parentElement) {
    const { overflowY } = getComputedStyle(p);
    if ((overflowY === "auto" || overflowY === "scroll") && p.scrollHeight > p.clientHeight) {
      return p;
    }
  }
  return null;
}

/** Scroll so that `el`'s top is back at `top` (a `getBoundingClientRect().top` from before). */
export function keepInPlace(el: HTMLElement, top: number): void {
  const moved = el.getBoundingClientRect().top - top;
  if (Math.abs(moved) < 1) return;
  const scroller = scrollingAncestor(el);
  if (scroller) scroller.scrollTop += moved;
  else window.scrollBy(0, moved);
}
