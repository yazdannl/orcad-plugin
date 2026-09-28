"""OpenSCAD backend for the orcad OrcaSlicer plugin (standard library only)."""
from .bootstrap import (ARTIFACTS, BOOTSTRAP_VERSION, artifact_for, cache_root, ensure_openscad,
                        openscad_bootstrap_status, start_openscad_bootstrap, wait_for_openscad)
from .catalog import CATALOG, LIBRARY_DIR, QUALITY_PROFILES, SOURCE_REVISION, defaults, object_spec, source_path
from .errors import BackendError, ErrorCode, ValidationError
from .mesh import IndexedMesh, index_stl, triangle_count, write_3mf
from .runner import (EngineInfo, OpenSCADRunner, RenderResult, backend_args, build_argv, child_env, clean_log,
                     discover_openscad, encode_define, probe_openscad)
from .validation import validate_parameters

__all__ = [
    "ARTIFACTS", "BOOTSTRAP_VERSION", "BackendError", "CATALOG", "EngineInfo", "ErrorCode", "IndexedMesh",
    "LIBRARY_DIR", "OpenSCADRunner", "QUALITY_PROFILES", "RenderResult", "SOURCE_REVISION", "ValidationError",
    "artifact_for", "backend_args", "build_argv", "cache_root", "child_env", "clean_log", "defaults", "discover_openscad",
    "encode_define", "ensure_openscad", "index_stl", "object_spec", "openscad_bootstrap_status",
    "probe_openscad", "source_path", "start_openscad_bootstrap", "triangle_count", "validate_parameters",
    "wait_for_openscad", "write_3mf",
]
