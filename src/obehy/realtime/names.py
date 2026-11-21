"""Stop names as sources write them, compared the one way the keyless inference and its replay
report both use. Pure."""

from __future__ import annotations


def same_name(a: str, b: str) -> bool:
    """Stop names compared without commas, spaces and case (ARRIVA-Q6: `Brno, Benešova
    tř.,hotel GRAND` is `Brno,Benešova tř.hotel GRAND`)."""

    return normal(a) == normal(b)


def normal(name: str) -> str:
    return name.replace(",", "").replace(" ", "").casefold()
