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
# written for it keeps resolving; the 1.x tree further down carries its own
# src/ next to its files, which OpenSCAD searches before OPENSCADPATH.
# Roots are relative to this directory.
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


def credit_for(name: str) -> tuple[str, dict[str, str]] | None:
    """The credit a model's card shows, and its shared id.

    A model under ``vendor/`` is credited to the library it came from; a model
    with its own ``attribution`` overrides that. Original models have none.
    """
    spec = object_spec(name)
    explicit = spec.get("attribution") if isinstance(spec.get("attribution"), dict) else None
    source = str(spec.get("source", ""))
    library_key = library = None
    if source.startswith("vendor/"):
        for key, entry in LIBRARIES.items():
            if source.startswith(f"{entry['root']}/"):
                library_key, library = key, entry
                break
    if not (explicit or {}).get("label") and library is None:
        return None
    credit = {
        "label": str((explicit or {}).get("label") or (library or {}).get("label", "")),
        "license": str((explicit or {}).get("license") or (library or {}).get("license", "")),
        "author": str((explicit or {}).get("author") or (library or {}).get("author", "")),
        "repository": str((explicit or {}).get("repository") or (library or {}).get("repository", "")),
    }
    return (library_key or f"object:{name}"), credit


def index_payload() -> dict[str, Any]:
    """The compact catalog the page ships with: what the model list needs.

    Descriptions and credits yes, parameters and presets no - those arrive one
    object at a time (``spec_payload``) so the page stays small enough for the
    host's webview, which takes the whole page in one string.
    """
    objects: dict[str, dict[str, Any]] = {}
    credits: dict[str, dict[str, str]] = {}
    for name, spec in CATALOG["objects"].items():
        entry: dict[str, Any] = {
            "label": spec["label"],
            "short": spec.get("short", ""),
            "category": spec.get("category", ""),
            "description": spec.get("description", ""),
        }
        if spec.get("tags"):
            entry["tags"] = list(spec["tags"])
        credited = credit_for(name)
        if credited is not None:
            credit_id, credit = credited
            credits.setdefault(credit_id, credit)
            entry["credit"] = credit_id
        objects[name] = entry
    return {"revision": LIBRARY_REVISION, "quality_profiles": QUALITY_PROFILES,
            "credits": credits, "objects": objects}


def spec_payload(name: str) -> dict[str, Any]:
    """One object's parameters, presets and credit id, sent when it is selected."""
    spec = object_spec(name)
    payload: dict[str, Any] = {
        "label": spec["label"],
        "short": spec.get("short", ""),
        "category": spec.get("category", ""),
        "description": spec.get("description", ""),
        "parameters": spec.get("parameters", []),
        "presets": spec.get("presets", []),
    }
    if spec.get("tags"):
        payload["tags"] = list(spec["tags"])
    credited = credit_for(name)
    if credited is not None:
        payload["credit"] = credited[0]
    return payload
