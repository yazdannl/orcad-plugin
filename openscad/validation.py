"""Strict catalog-driven parameter validation (the bridge's trust boundary)."""
from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .catalog import defaults, parameter_specs
from .errors import ValidationError


def _check_value(key: str, spec: Mapping[str, Any], value: Any) -> None:
    label = spec.get("label", key)
    if spec["type"] == "boolean":
        if type(value) is not bool:
            raise ValidationError(f"{label} must be on or off", key)
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValidationError(f"{label} must be a number", key)
    if spec["type"] == "integer" and type(value) is not int:
        raise ValidationError(f"{label} must be a whole number", key)
    unit = f" {spec['unit']}" if spec.get("unit") else ""
    if "min" in spec and value < spec["min"]:
        raise ValidationError(f"{label} must be at least {spec['min']:g}{unit}", key)
    if "max" in spec and value > spec["max"]:
        raise ValidationError(f"{label} must be at most {spec['max']:g}{unit}", key)
    step, minimum = spec.get("step"), spec.get("min", 0)
    if step and not math.isclose((value - minimum) / step, round((value - minimum) / step), abs_tol=1e-6):
        raise ValidationError(f"{label} must be a multiple of {step:g}", key)
    options = spec.get("options")
    if options and value not in {option["value"] for option in options}:
        raise ValidationError(f"{label} is not a supported option", key)


def validate_parameters(name: str, values: Mapping[str, Any]) -> dict[str, Any]:
    """Return defaults overlaid with *values*; raise ValidationError on bad input."""
    if not isinstance(values, Mapping):
        raise ValidationError("parameters must be an object")
    specs = parameter_specs(name)
    unknown = sorted(set(values) - set(specs))
    if unknown:
        raise ValidationError(f"Unknown parameter {unknown[0]}", *unknown)
    result = defaults(name)
    result.update(values)
    for key, spec in specs.items():
        _check_value(key, spec, result[key])
    _check_rules(name, result)
    return result


def _check_rules(name: str, p: dict[str, Any]) -> None:
    if name == "gridfinity_bin":
        if p["refined_holes"] and p["magnet_holes"]:
            raise ValidationError("Refined holes and magnet holes cannot both be enabled", "refined_holes", "magnet_holes")
        if (p["divx"] == 0) != (p["divy"] == 0):
            raise ValidationError("Divisions X and Y must both be zero (solid) or both positive", "divx", "divy")
        if p["cut_cylinders"] and p["c_chamfer"] > p["cd"] / 2:
            raise ValidationError("Cylinder chamfer cannot exceed half the cylinder diameter", "c_chamfer", "cd")
        if p["depth"] and p["height_internal"] and p["depth"] > p["height_internal"]:
            raise ValidationError("Compartment depth cannot exceed the internal height override", "depth", "height_internal")
    elif name == "gridfinity_baseplate":
        if p["gridx"] == 0 and p["distancex"] == 0:
            raise ValidationError("Set Grid X or Minimum X", "gridx", "distancex")
        if p["gridy"] == 0 and p["distancey"] == 0:
            raise ValidationError("Set Grid Y or Minimum Y", "gridy", "distancey")
    elif name == "tube":
        if p["inner_diameter"] >= p["outer_diameter"]:
            raise ValidationError("Inner diameter must be smaller than the outer diameter", "inner_diameter", "outer_diameter")
    elif name == "bracket":
        if p["hole_diameter"] >= p["width"]:
            raise ValidationError("Holes must be narrower than the plate", "hole_diameter", "width")
        if p["hole_spacing"] + p["hole_diameter"] >= p["length"]:
            raise ValidationError("Holes must fit inside the plate length", "hole_spacing", "hole_diameter", "length")
