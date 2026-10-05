"""Canonical object catalog; the JSON file is shared with the frontend build."""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from .errors import BackendError, ErrorCode

ROOT = Path(__file__).resolve().parent
CATALOG: dict[str, Any] = json.loads((ROOT / "catalog.json").read_text(encoding="utf-8"))
SOURCE_REVISION: str = CATALOG["source"]["revision"]
QUALITY_PROFILES: dict[str, dict[str, float]] = CATALOG["quality_profiles"]
# Vendored OpenSCAD libraries, in the order they must appear on OPENSCADPATH.
# The first entry is the Gridfinity Rebuilt tree, so `include <src/core/...>`
# written for it keeps resolving. Roots are relative to this directory.
LIBRARIES: dict[str, dict[str, Any]] = CATALOG.get("libraries", {})
LIBRARY_DIRS: dict[str, Path] = {name: ROOT / spec["root"] for name, spec in LIBRARIES.items()}
VENDOR_DIR: Path = ROOT / "vendor"
# Every library root, then vendor/ itself so a wrapper can address one library
# unambiguously as <rackstack-8e296e93/rack-mount/tray/tray.scad>.
LIBRARY_SEARCH_PATH: list[Path] = [*LIBRARY_DIRS.values(), VENDOR_DIR]
# Code mode resolves `include <src/...>` against the vendored Gridfinity tree.
LIBRARY_DIR: Path = next(iter(LIBRARY_DIRS.values())) if LIBRARY_DIRS else \
    ROOT / "vendor" / "gridfinity-rebuilt-openscad-910e22d8"
# Every vendored revision together, so one change cannot reuse another's cache entries.
LIBRARY_REVISION: str = hashlib.sha256(
    "|".join(f"{name}={spec['revision']}" for name, spec in LIBRARIES.items()).encode()).hexdigest()


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
