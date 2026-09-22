"""Strict catalog-driven parameter validation."""

from __future__ import annotations

import math
from collections.abc import Mapping
from typing import Any

from .catalog import defaults, object_spec, parameter_specs
from .errors import ValidationError


def _number(value: Any, *, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValidationError(f"{name} must be a finite number", parameter=name)
    value = float(value)
    if not math.isfinite(value):
        raise ValidationError(f"{name} must be finite", parameter=name)
    return value


def _check_step(value: float, spec: Mapping[str, Any]) -> None:
    step = spec.get("step")
    minimum = spec.get("min")
    if not step or minimum is None:
        return
    quotient = (value - float(minimum)) / float(step)
    if not math.isclose(quotient, round(quotient), rel_tol=0, abs_tol=1e-8):
        raise ValidationError(
            f"{spec['variable']} must use step {step}",
            parameter=spec["variable"], value=value, step=step,
        )


def validate_parameters(name: str, values: Mapping[str, Any], *, apply_defaults: bool = True) -> dict[str, Any]:
    """Validate and return a new parameter mapping without coercion."""
    if not isinstance(values, Mapping):
        raise ValidationError("parameters must be an object")
    specs = parameter_specs(name)
    unknown = sorted(set(values) - set(specs))
    if unknown:
        raise ValidationError("unknown parameter", parameter=unknown[0], unknown=unknown)
    result = defaults(name) if apply_defaults else dict(values)
    if not apply_defaults:
        missing = sorted(set(specs) - set(values))
        if missing:
            raise ValidationError("missing parameter", parameter=missing[0], missing=missing)
    result.update(values)

    for key, spec in specs.items():
        value = result[key]
        kind = spec["type"]
        if kind == "boolean":
            if type(value) is not bool:
                raise ValidationError(f"{key} must be a boolean", parameter=key, value=value)
            continue
        number = _number(value, name=key)
        if kind == "integer" and type(value) is not int:
            raise ValidationError(f"{key} must be an integer", parameter=key, value=value)
        if "min" in spec and number < spec["min"]:
            raise ValidationError(f"{key} is below its minimum", parameter=key, value=value, minimum=spec["min"])
        if "max" in spec and number > spec["max"]:
            raise ValidationError(f"{key} is above its maximum", parameter=key, value=value, maximum=spec["max"])
        _check_step(number, spec)
        options = spec.get("options")
        if options and value not in {option["value"] for option in options}:
            raise ValidationError(f"{key} is not a supported option", parameter=key, value=value)

    if name == "bin":
        if result["refined_holes"] and result["magnet_holes"]:
            raise ValidationError("refined_holes and magnet_holes cannot both be enabled", parameters=["refined_holes", "magnet_holes"])
        if (result["divx"] == 0) != (result["divy"] == 0):
            raise ValidationError("divx and divy must both be zero or both be positive", parameters=["divx", "divy"])
        if result["cut_cylinders"] and result["c_chamfer"] > result["cd"] / 2:
            raise ValidationError("c_chamfer cannot exceed half the cylinder diameter", parameters=["c_chamfer", "cd"])
        if result["depth"] and result["height_internal"] and result["depth"] > result["height_internal"]:
            raise ValidationError("depth cannot exceed the internal height override", parameters=["depth", "height_internal"])
    elif name == "baseplate":
        if result["gridx"] == 0 and result["distancex"] == 0:
            raise ValidationError("gridx or distancex must be positive", parameters=["gridx", "distancex"])
        if result["gridy"] == 0 and result["distancey"] == 0:
            raise ValidationError("gridy or distancey must be positive", parameters=["gridy", "distancey"])
    else:
        object_spec(name)
    return result


# Short alias for callers that prefer the catalog's verb.
validate = validate_parameters
