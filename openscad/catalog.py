"""Canonical object catalog; the JSON file is shared with the frontend build."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .errors import BackendError, ErrorCode

ROOT = Path(__file__).resolve().parent
CATALOG: dict[str, Any] = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
SOURCE_REVISION: str = CATALOG["source"]["revision"]
QUALITY_PROFILES: dict[str, dict[str, float]] = CATALOG["quality_profiles"]
# Code mode resolves `include <src/...>` against the vendored Gridfinity tree.
LIBRARY_DIR = ROOT / "vendor" / "gridfinity-rebuilt-openscad-910e22d8"


def object_spec(name: str) -> dict[str, Any]:
    try:
        return CATALOG["objects"][name]
    except (KeyError, TypeError):
        raise BackendError(ErrorCode.INVALID_OBJECT, f"Unknown object: {name}", {"object": name}) from None


def parameter_specs(name: str) -> dict[str, dict[str, Any]]:
    return {item["variable"]: item for item in object_spec(name)["parameters"]}


def defaults(name: str) -> dict[str, Any]:
    return {key: item["default"] for key, item in parameter_specs(name).items()}


def source_path(name: str) -> Path:
    return ROOT / object_spec(name)["source"]
