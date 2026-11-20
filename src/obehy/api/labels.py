"""How names and codes read, decided once for every client (the web and the app showed each
their own way).

- A colour as CSS and Android read it: `#RRGGBB` (feeds give it bare).
- A Czech licence plate as written, not as compared: three characters and four ("1TD 8408"),
  an electric car's `EL` apart ("EL 928FK"); any other as given.
- A vehicle's name: its fleet number, else the registry's public label, else the source's ID.
- A trip's stops on a map: JDF names read "Town,Part,Place"; when more than half of the trip's
  stops are in one town, that town is left out of their names ("Můstek"), the rest starting with
  a capital; other towns' stops keep the whole name ("Kolín, Zálabí"). Names without a comma
  (trains, villages) stay as they are.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Sequence

_PLATE = re.compile(r"^([0-9][A-Z0-9]{2})([0-9]{4})$")
_ELECTRIC = re.compile(r"^EL([0-9]{3}[A-Z]{2})$")
# More than this share of a trip's stops in one town makes it the trip's home town.
HOME_TOWN_SHARE = 0.5


def colour(value: str | None) -> str | None:
    """A colour as `#RRGGBB`."""

    if not value:
        return None
    return value if value.startswith("#") else f"#{value}"


def plate(value: str | None) -> str | None:
    """A licence plate as written."""

    if value is None:
        return None
    if found := _PLATE.match(value):
        return f"{found[1]} {found[2]}"
    if found := _ELECTRIC.match(value):
        return f"EL {found[1]}"
    return value


def vehicle_name(fleet_number: str | None, public_label: str | None, source_vehicle_id: str) -> str:
    """A vehicle as its operator numbers it, else as the registry or its source names it."""

    return fleet_number or public_label or source_vehicle_id


def stop_labels(names: Sequence[str]) -> list[str]:
    """A trip's stop names as its map labels them (the home town left out)."""

    parts = [[p.strip() for p in name.split(",") if p.strip()] for name in names]
    towns = Counter(p[0] for p in parts if len(p) > 1)
    home = next((t for t, n in towns.items() if n > len(names) * HOME_TOWN_SHARE), None)
    labels: list[str] = []
    for name, p in zip(names, parts, strict=True):
        if len(p) <= 1:
            labels.append(p[0] if p else name)
        elif p[0] != home:
            labels.append(", ".join(p))
        else:
            rest = ", ".join(p[1:])
            labels.append(rest[:1].upper() + rest[1:])
    return labels
