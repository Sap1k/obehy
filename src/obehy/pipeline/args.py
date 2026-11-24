"""Command-line value types shared by the build commands."""

from __future__ import annotations

import argparse
import re
from typing import Literal

JobSetting = Literal["auto"] | int
YearSetting = Literal["auto"] | int

# A build run: its UTC start and a random suffix; release tags are build-<run id>.
RUN_ID = re.compile(r"^\d{8}T\d{6}Z-[0-9a-f]{12}$")


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


def parse_run_id(value: str) -> str:
    if not RUN_ID.match(value):
        raise argparse.ArgumentTypeError("must look like 20261124T070233Z-1bda58bef584")
    return value
