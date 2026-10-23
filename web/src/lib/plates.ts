// Licence plates (SPZ). The API gives them as compared: upper case, no spaces ("1TD8408").
// They read, and seznam-autobusu.cz finds them, only as written: "1TD 8408", "EL 928FK".

/** A Czech plate as written: three characters and four ("1TD 8408"), an electric car's "EL"
 * apart ("EL 928FK"); any other as given. */
export function plateText(plate: string): string {
  const standard = /^([0-9][A-Z0-9]{2})([0-9]{4})$/.exec(plate);
  if (standard) return `${standard[1]} ${standard[2]}`;
  const electric = /^EL([0-9]{3}[A-Z]{2})$/.exec(plate);
  if (electric) return `EL ${electric[1]}`;
  return plate;
}

/** The vehicle on seznam-autobusu.cz: its search by plate, which opens the vehicle's page when
 * it has one. */
export function plateUrl(plate: string): string {
  return `https://seznam-autobusu.cz/seznam?spz=${encodeURIComponent(plateText(plate))}`;
}
