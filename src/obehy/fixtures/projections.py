from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast


def load_projection(path: Path) -> dict[str, Any]:
    with path.open(encoding="utf-8") as stream:
        value: object = json.load(stream)
    if not isinstance(value, dict):
        raise ValueError(f"Fixture projection must be a JSON object: {path}")
    return cast(dict[str, Any], value)
