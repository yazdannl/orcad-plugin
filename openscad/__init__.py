"""OpenSCAD backend for the orcad OrcaSlicer plugin (standard library only)."""
# The release this backend belongs to. orcad.py compares it with its own version
# before using a backend folder that lies beside it: a page from one release and
# a backend from another cannot import against each other.
BACKEND_VERSION = "0.9.27"
from .bootstrap import (ARTIFACTS, BOOTSTRAP_VERSION, SNAPSHOT_INDEX, artifact_for, cache_root,
                        ensure_openscad, installed_engine, openscad_bootstrap_status,
                        start_openscad_bootstrap, wait_for_openscad)
from .catalog import (CATALOG, LIBRARIES, LIBRARY_DIR, LIBRARY_DIRS, LIBRARY_REVISION, QUALITY_PROFILES,
                      SOURCE_REVISION, credit_for, defaults, index_payload, object_spec, source_path, spec_payload)
from .errors import BackendError, ErrorCode, ValidationError
from .mesh import IndexedMesh, index_stl, triangle_count, write_3mf
from . import mesh_native
from .runner import (DEVNULL_IN, DEVNULL_OUT, EngineInfo, OpenSCADRunner, RenderResult, backend_args,
                     build_argv, child_env, clean_log,
                     discover_openscad, encode_define, probe_openscad, program)
from .validation import validate_parameters

__all__ = [
    "ARTIFACTS", "BACKEND_VERSION", "BOOTSTRAP_VERSION", "SNAPSHOT_INDEX", "BackendError", "CATALOG", "EngineInfo", "ErrorCode", "IndexedMesh",
    "LIBRARIES", "LIBRARY_DIR", "LIBRARY_DIRS", "LIBRARY_REVISION", "OpenSCADRunner", "QUALITY_PROFILES",
    "RenderResult", "SOURCE_REVISION", "ValidationError",
    "artifact_for", "backend_args", "build_argv", "cache_root", "child_env", "clean_log", "defaults", "discover_openscad",
    "DEVNULL_IN", "DEVNULL_OUT", "encode_define", "ensure_openscad", "installed_engine", "index_stl", "index_payload", "object_spec", "openscad_bootstrap_status",
    "probe_openscad", "program", "source_path", "spec_payload", "credit_for", "start_openscad_bootstrap", "triangle_count", "validate_parameters",
    "wait_for_openscad", "write_3mf",
]
