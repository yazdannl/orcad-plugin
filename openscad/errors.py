"""Stable error values shared by the OpenSCAD backend."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


class ErrorCode:
    INVALID_OBJECT = "invalid_object"
    INVALID_PARAMETERS = "invalid_parameters"
    INVALID_PARAMETER = "invalid_parameter"
    INVALID_VALUE = "invalid_value"
    UNSUPPORTED_VERSION = "openscad_unsupported_version"
    NOT_FOUND = "openscad_not_found"
    PROBE_FAILED = "openscad_probe_failed"
    SOURCE_NOT_FOUND = "source_not_found"
    OUTPUT_ERROR = "output_error"
    TIMEOUT = "timeout"
    CANCELLED = "cancelled"
    PROCESS_FAILED = "process_failed"
    INVALID_STL = "invalid_stl"


@dataclass
class BackendError(Exception):
    """An error that is safe to turn into a JSON result."""

    code: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "message": self.message, "details": self.details}


class ValidationError(BackendError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(ErrorCode.INVALID_PARAMETERS, message, details)


class InvalidSTLError(BackendError):
    def __init__(self, message: str, **details: Any) -> None:
        super().__init__(ErrorCode.INVALID_STL, message, details)
