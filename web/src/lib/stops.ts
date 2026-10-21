/** "nástupiště 1–5" from the boarding points' codes; null when they have none. */
export function platformsText(codes: readonly (string | null)[]): string | null {
  const unique = [...new Set(codes.filter((c): c is string => Boolean(c)))];
  if (unique.length === 0) return null;
  if (unique.length === 1) return `nástupiště ${unique[0]}`;
  if (unique.every((c) => /^\d+$/.test(c))) {
    const numbers = unique.map(Number);
    return `nástupiště ${Math.min(...numbers)}–${Math.max(...numbers)}`;
  }
  const sorted = unique.sort((a, b) => a.localeCompare(b, "cs", { numeric: true }));
  return `nástupiště ${sorted.slice(0, 4).join(", ")}${sorted.length > 4 ? ", …" : ""}`;
}
