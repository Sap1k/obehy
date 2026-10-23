import { useState } from "react";

// A reader's small preferences (the navbar collapsed, the notes open). Storage can be blocked or
// unreadable: then the flag starts off and is not remembered.

type Kind = "local" | "session";

function storage(kind: Kind): Storage {
  return kind === "local" ? localStorage : sessionStorage;
}

function read(kind: Kind, key: string): boolean {
  try {
    return storage(kind).getItem(key) === "1";
  } catch {
    return false;
  }
}

/** A flag remembered in local (across visits) or session storage. */
export function usePersistentFlag(key: string, kind: Kind): [boolean, (on: boolean) => void] {
  const [on, setOn] = useState(() => read(kind, key));
  const set = (next: boolean) => {
    setOn(next);
    try {
      storage(kind).setItem(key, next ? "1" : "0");
    } catch {
      // Not remembered.
    }
  };
  return [on, set];
}
