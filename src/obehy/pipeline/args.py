"""Command-line value types shared by the build commands."""

from __future__ import annotations

import argparse
import re
from typing import Literal

JobSetting = Literal["auto"] | int
YearSetting = Literal["auto"] | int


def parse_jobs(value: str) -> JobSetting:
    if value.casefold() == "auto":
        return "auto"
    try:
        parsed = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be 'auto' or a positive integer") from error
    if parsed <= 0:
        raise argparse.ArgumentTypeError("must be 'auto' or a positive integer")
    return parsed


def parse_memory_budget(value: str) -> str:
    if not re.fullmatch(r"(?i)(?:auto|[0-9]+(?:\.[0-9]+)?(?:KiB|MiB|GiB))", value):
        raise argparse.ArgumentTypeError("must be 'auto' or a size such as 10GiB")
    return value


def parse_year(value: str) -> YearSetting:
    if value.casefold() == "auto":
        return "auto"
    try:
        year = int(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be 'auto' or a four-digit year") from error
    if not 2000 <= year <= 9999:
        raise argparse.ArgumentTypeError("must be 'auto' or a four-digit year")
    return year
