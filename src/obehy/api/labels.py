"""How names and codes read, decided once for every client (the web and the app showed each
their own way).

- A colour as CSS and Android read it: `#RRGGBB` (feeds give it bare).
- A Czech licence plate as written, not as compared: three characters and four ("1TD 8408"),
  an electric car's `EL` apart ("EL 928FK"); any other as given.
- A vehicle's name: its fleet number, else the registry's public label, else the source's ID.
"""

from __future__ import annotations

import re

_PLATE = re.compile(r"^([0-9][A-Z0-9]{2})([0-9]{4})$")
_ELECTRIC = re.compile(r"^EL([0-9]{3}[A-Z]{2})$")


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
