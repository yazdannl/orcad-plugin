"""Canonical Gridfinity/OpenSCAD catalog.

The JSON file is deliberately the source of truth so a host bridge can consume
it without importing the runner.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import ErrorCode, BackendError

CATALOG_PATH = Path(__file__).with_name("catalog.json")
CATALOG: dict[str, Any] = json.loads(CATALOG_PATH.read_text(encoding="utf-8"))
SOURCE_REVISION = CATALOG["source"]["revision"]
QUALITY_PROFILES = CATALOG["quality_profiles"]


def object_spec(name: str) -> dict[str, Any]:
    try:
        return CATALOG["objects"][name]
    except KeyError as exc:
        raise BackendError(ErrorCode.INVALID_OBJECT, f"Unknown OpenSCAD object: {name}", {"object": name}) from exc


def parameter_specs(name: str) -> dict[str, dict[str, Any]]:
    return {item["variable"]: item for item in object_spec(name)["parameters"]}


def defaults(name: str) -> dict[str, Any]:
    return {key: item["default"] for key, item in parameter_specs(name).items()}


def source_path(name: str) -> Path:
    return Path(__file__).with_name("vendor") / "gridfinity-rebuilt-openscad-910e22d8" / object_spec(name)["entry_file"]
