// Where the reader came from, when a link says so (`?refer=dukfinder`: the old DÚKfinder's
// address redirects here). Read once when the app starts and taken out of the address, so it
// is not shared on or kept in bookmarks. Imported first (main.tsx), before the router reads
// the address.

const PARAM = "refer";

function take(): string | null {
  try {
    const url = new URL(window.location.href);
    const from = url.searchParams.get(PARAM);
    if (from === null) return null;
    url.searchParams.delete(PARAM);
    window.history.replaceState(window.history.state, "", url);
    return from;
  } catch {
    return null;
  }
}

export const referral: string | null = take();
