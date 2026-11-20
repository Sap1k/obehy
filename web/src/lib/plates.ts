// Licence plates come from the API as written ("1TD 8408"); seznam-autobusu.cz finds them so.

/** The vehicle on seznam-autobusu.cz: its search by plate, which opens the vehicle's page when
 * it has one. */
export function plateUrl(plate: string): string {
  return `https://seznam-autobusu.cz/seznam?spz=${encodeURIComponent(plate)}`;
}
