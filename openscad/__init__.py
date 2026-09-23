"""Reusable OpenSCAD/Gridfinity backend for the React CAD frontend."""

from .bootstrap import (
    ARTIFACTS,
    BOOTSTRAP_VERSION,
    artifact_for,
    cache_root,
    ensure_openscad,
    openscad_bootstrap_status,
    start_openscad_bootstrap,
    wait_for_openscad,
)
from .cache import cache_key, cache_payload
from .catalog import CATALOG, QUALITY_PROFILES, SOURCE_REVISION, defaults, object_spec
from .errors import BackendError, ErrorCode, InvalidSTLError, ValidationError
from .runner import (
    EngineInfo,
    OpenSCADRunner,
    RenderRequest,
    RenderResult,
    build_argv,
    compatibility_smoke_test,
    discover_openscad,
    encode_define,
    probe_openscad,
)
from .stl import Mesh, encode_binary, encode_binary_stl, mesh_stats, parse_binary, parse_binary_stl, read_binary_stl
from .validation import validate_parameters

__all__ = [
    "ARTIFACTS", "BackendError", "BOOTSTRAP_VERSION", "CATALOG", "EngineInfo", "ErrorCode", "InvalidSTLError", "Mesh",
    "OpenSCADRunner", "QUALITY_PROFILES", "RenderRequest", "RenderResult", "SOURCE_REVISION",
    "ValidationError", "build_argv", "cache_key", "cache_payload", "compatibility_smoke_test",
    "defaults", "discover_openscad", "encode_binary", "encode_binary_stl", "encode_define", "mesh_stats",
    "artifact_for", "cache_root", "ensure_openscad", "object_spec", "openscad_bootstrap_status",
    "parse_binary", "parse_binary_stl", "probe_openscad", "read_binary_stl", "start_openscad_bootstrap",
    "validate_parameters", "wait_for_openscad",
]
