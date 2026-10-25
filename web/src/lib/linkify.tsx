import { IconExternalLink } from "@tabler/icons-react";
import type { ReactNode } from "react";

// Contacts in a timetable note as links: e-mail addresses (mailto:), web addresses (with or
// without http, or a bare domain on a common TLD; opened in a new tab and marked as leaving
// the site) and Czech phone numbers, nine digits in threes with an optional +420 (tel:, with
// +420 added when missing).

const CONTACT = new RegExp(
  [
    /([\w.+-]+@[\w-]+(?:\.[\w-]+)+)/.source,
    /((?:https?:\/\/|www\.)[^\s<>()]+|(?<![\w@.-])(?:[\w-]+\.)+(?:cz|sk|eu|com|org|net|info|de|at|pl)\b(?:\/[^\s<>()]*)?)/
      .source,
    /((?:\+\d{3}\s?)?\d{3}\s?\d{3}\s?\d{3})(?!\d)/.source,
  ].join("|"),
  "g",
);
// Sentence punctuation after an address is not part of it.
const TRAILING = /[.,;:!?)]+$/;

export type LinkKind = "mail" | "web" | "phone";

export function linkify(text: string, className?: string): ReactNode[] {
  const out: ReactNode[] = [];
  let at = 0;
  for (const m of text.matchAll(CONTACT)) {
    const start = m.index;
    // A number glued to more digits before it (an account, a long code) is no phone.
    if (m[3] && start > 0 && /\d/.test(text[start - 1]!)) continue;
    const found = m[2] ? m[0].replace(TRAILING, "") : m[0];
    if (start > at) out.push(text.slice(at, start));
    const kind: LinkKind = m[1] ? "mail" : m[2] ? "web" : "phone";
    out.push(
      kind === "web" ? (
        <a
          key={start}
          href={/^https?:\/\//.test(found) ? found : `https://${found}`}
          className={className}
          data-kind={kind}
          target="_blank"
          rel="noreferrer"
        >
          {found}
          <IconExternalLink size={12} stroke={2} aria-label="(otevře jiný web)" />
        </a>
      ) : (
        <a
          key={start}
          href={kind === "mail" ? `mailto:${found}` : `tel:${phoneNumber(found)}`}
          className={className}
          data-kind={kind}
        >
          {found}
        </a>
      ),
    );
    at = start + found.length;
  }
  if (at < text.length) out.push(text.slice(at));
  return out;
}

function phoneNumber(found: string): string {
  const digits = found.replace(/[^\d+]/g, "");
  return digits.startsWith("+") ? digits : `+420${digits}`;
}
