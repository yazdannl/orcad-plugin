"""OpenSCAD discovery, probing, and bounded, cancellable, cached rendering."""
from __future__ import annotations

import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .catalog import LIBRARY_DIR, QUALITY_PROFILES, SOURCE_REVISION, source_path
from .errors import BackendError, ErrorCode
from .mesh import triangle_count
from .validation import validate_parameters

MIN_VERSION = (2023, 0)
MAX_CODE_CHARS = 200_000
_VERSION_RE = re.compile(r"OpenSCAD version\s+(\d+\.\d+[0-9A-Za-z.+-]*)", re.I)
_LINE_RE = re.compile(r"in file [^,]*, line (\d+)")
_NOISE = ("Geometries in cache", "Geometry cache size", "CGAL Polyhedrons", "CGAL cache size",
          "Total rendering time", "Top level object is", "   ", "Rendering Polygon Mesh",
          "Parsing design", "Compiling design", "Rendering finished", "Normalized tree",
          "Simple:", "Convex:", "Vertices:", "Facets:", "Triangles:", "Volumes:")
# Host variables (e.g. from an OrcaSlicer AppImage) that break other executables.
_HOST_ENV = ("LD_LIBRARY_PATH", "LD_PRELOAD", "PYTHONHOME", "PYTHONPATH", "QT_PLUGIN_PATH",
             "QT_QPA_PLATFORM_PLUGIN_PATH", "GDK_PIXBUF_MODULE_FILE", "GIO_MODULE_DIR",
             "GSETTINGS_SCHEMA_DIR", "APPDIR", "APPIMAGE", "ARGV0", "OWD", "DYLD_LIBRARY_PATH",
             "DYLD_FRAMEWORK_PATH")
Cancel = Callable[[], bool] | None


def child_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _HOST_ENV}
    env["OPENSCADPATH"] = str(LIBRARY_DIR)
    return env


def popen_flags() -> dict[str, Any]:
    """Keep Windows from flashing a console window for every render."""
    return {"creationflags": getattr(subprocess, "CREATE_NO_WINDOW", 0)} if sys.platform == "win32" else {}


@dataclass(frozen=True)
class EngineInfo:
    executable: str
    version: str | None
    supported: bool
    warning: str | None = None

    def to_dict(self) -> dict[str, object]:
        return {"executable": self.executable, "version": self.version,
                "supported": self.supported, "warning": self.warning}


def discover_openscad(executable: str | os.PathLike[str] | None = None) -> Path:
    candidate = str(executable) if executable is not None else shutil.which("openscad")
    if not candidate:
        raise BackendError(ErrorCode.NOT_FOUND, "OpenSCAD executable was not found")
    path = Path(shutil.which(candidate) or candidate).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise BackendError(ErrorCode.NOT_FOUND, "OpenSCAD executable is not runnable", {"executable": str(path)})
    return path


def version_tuple(version: str) -> tuple[int, int]:
    match = re.match(r"(\d+)\.(\d+)", version)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def probe_openscad(executable: str | os.PathLike[str] | None = None, *, timeout: float = 15.0) -> EngineInfo:
    path = discover_openscad(executable)
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, check=False,
                                timeout=timeout, env=child_env(), **popen_flags())
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendError(ErrorCode.PROBE_FAILED, "OpenSCAD could not be started", {"executable": str(path)}) from exc
    match = _VERSION_RE.search(f"{result.stdout}\n{result.stderr}")
    if result.returncode or not match:
        raise BackendError(ErrorCode.PROBE_FAILED, "OpenSCAD did not report a version", {"returncode": result.returncode})
    version = match.group(1)
    supported = version_tuple(version) >= MIN_VERSION
    warning = None if supported else f"OpenSCAD {version} is too old; version 2023 or newer is required."
    return EngineInfo(str(path), version, supported, warning)


def encode_define(name: str, value: Any) -> str:
    if not re.fullmatch(r"\$?[A-Za-z_][A-Za-z0-9_]*", name):
        raise ValueError("unsafe OpenSCAD variable name")
    if type(value) is bool:
        encoded = "true" if value else "false"
    elif type(value) is int:
        encoded = str(value)
    elif type(value) is float and math.isfinite(value):
        encoded = format(value, ".15g")
    else:
        raise ValueError("OpenSCAD -D values must be finite numbers or booleans")
    return f"{name}={encoded}"


def backend_args(version: str | None) -> list[str]:
    """Prefer the Manifold kernel: ~50x faster than CGAL for Gridfinity models."""
    year = version_tuple(version or "")[0]
    if year >= 2024:
        return ["--backend=Manifold"]
    return ["--enable=manifold"] if year == 2023 else []


def build_argv(executable: str | os.PathLike[str], source: Path, output: Path,
               defines: dict[str, Any], quality: str, version: str | None) -> list[str]:
    if quality not in QUALITY_PROFILES:
        raise BackendError(ErrorCode.INVALID_VALUE, f"Unknown quality profile {quality!r}")
    profile = QUALITY_PROFILES[quality]
    argv = [str(executable), "-o", str(output), "--export-format", "binstl", *backend_args(version)]
    for name, value in {**defines, "$fa": profile["fa"], "$fs": profile["fs"]}.items():
        argv += ["-D", encode_define(name, value)]
    return [*argv, str(source)]


def clean_log(text: str, source: Path | None = None) -> list[str]:
    lines = []
    for line in text.splitlines():
        if not line.strip() or line.startswith(_NOISE):
            continue
        if source is not None:
            line = line.replace(f"'{source}'", "your code").replace(str(source), "your code")
        lines.append(line.rstrip())
    return lines[-200:]


def _failure(log: list[str], returncode: int) -> BackendError:
    errors = [line for line in log if line.startswith("ERROR")] or \
             [line for line in log if "top level object" in line.lower() or "can't parse" in line.lower()]
    message = errors[0].removeprefix("ERROR:").strip() if errors else f"OpenSCAD exited with code {returncode}"
    if "top level object is empty" in message.lower():
        message = "The model is empty"
    elif "not a 3d object" in message.lower():
        message = "The result is 2D; extrude it (for example with linear_extrude) to get a printable solid"
    details: dict[str, Any] = {"returncode": returncode, "log": log}
    line = next((m.group(1) for m in map(_LINE_RE.search, errors) if m), None)
    if line:
        details["line"] = int(line)
        message = _LINE_RE.sub(f"on line {line}", message)
    return BackendError(ErrorCode.PROCESS_FAILED, message, details)


@dataclass
class RenderResult:
    stl: Path
    cached: bool
    duration_ms: int
    log: list[str] = field(default_factory=list)
    triangles: int = 0


class OpenSCADRunner:
    """Serial renderer with a size-bounded, content-addressed STL cache."""

    def __init__(self, executable: str | os.PathLike[str] | None = None, *,
                 cache_dir: str | os.PathLike[str], cache_limit: int = 512 * 1024 * 1024):
        self.engine = probe_openscad(executable)
        if not self.engine.supported:
            raise BackendError(ErrorCode.UNSUPPORTED_VERSION, self.engine.warning or "unsupported OpenSCAD",
                               self.engine.to_dict())
        self.cache_dir = Path(cache_dir)
        self.cache_limit = cache_limit

    def render_object(self, name: str, params: dict[str, Any], quality: str = "balanced", *,
                      timeout: float = 180.0, cancel: Cancel = None) -> RenderResult:
        checked = validate_parameters(name, params)
        identity = {"object": name, "params": checked}
        return self._render(source_path(name), checked, quality, identity, timeout, cancel)

    def render_code(self, code: str, quality: str = "balanced", *,
                    timeout: float = 180.0, cancel: Cancel = None) -> RenderResult:
        if not isinstance(code, str) or not code.strip():
            raise BackendError(ErrorCode.INVALID_VALUE, "The code is empty")
        if len(code) > MAX_CODE_CHARS:
            raise BackendError(ErrorCode.INVALID_VALUE, "The code is too large (over 200,000 characters)")
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="code-", dir=self.cache_dir) as work:
            source = Path(work) / "model.scad"
            source.write_text(code, encoding="utf-8")
            return self._render(source, {}, quality, {"code": code}, timeout, cancel)

    def _render(self, source: Path, defines: dict[str, Any], quality: str, identity: dict[str, Any],
                timeout: float, cancel: Cancel) -> RenderResult:
        started = time.monotonic()
        payload = {**identity, "quality": quality, "engine": self.engine.version, "library": SOURCE_REVISION}
        key = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        stl, log_path = self.cache_dir / f"{key}.stl", self.cache_dir / f"{key}.log"
        if stl.is_file():
            try:
                triangles = triangle_count(stl.read_bytes())
                os.utime(stl)
                log = log_path.read_text(encoding="utf-8").splitlines() if log_path.is_file() else []
                return RenderResult(stl, True, _elapsed(started), log, triangles)
            except (BackendError, OSError):
                stl.unlink(missing_ok=True)

        temp = self.cache_dir / f".{key}.{os.getpid()}.tmp.stl"
        argv = build_argv(self.engine.executable, source, temp, defines, quality, self.engine.version)
        try:
            with tempfile.TemporaryFile(dir=self.cache_dir) as stderr:
                returncode = _run(argv, source.parent, stderr, timeout, cancel)
                stderr.seek(0)
                log = clean_log(stderr.read().decode("utf-8", "replace"), source)
            if returncode != 0:
                raise _failure(log, returncode)
            if not temp.is_file():
                raise BackendError(ErrorCode.OUTPUT_ERROR, "OpenSCAD did not produce an STL")
            triangles = triangle_count(temp.read_bytes())
            os.replace(temp, stl)
            log_path.write_text("\n".join(log), encoding="utf-8")
        finally:
            temp.unlink(missing_ok=True)
        self._prune()
        return RenderResult(stl, False, _elapsed(started), log, triangles)

    def _prune(self) -> None:
        entries = sorted((entry.stat().st_mtime, entry.stat().st_size, Path(entry.path))
                         for entry in os.scandir(self.cache_dir) if entry.name.endswith(".stl") and entry.is_file())
        total = sum(size for _, size, _ in entries)
        for _, size, path in entries:
            if total <= self.cache_limit:
                break
            path.unlink(missing_ok=True)
            path.with_suffix(".log").unlink(missing_ok=True)
            total -= size


def _run(argv: list[str], cwd: Path, stderr, timeout: float, cancel: Cancel) -> int:
    started = time.monotonic()
    process = subprocess.Popen(argv, cwd=cwd, stdin=subprocess.DEVNULL, stdout=stderr, stderr=stderr,
                               env=child_env(), **popen_flags())
    try:
        while True:
            try:
                return process.wait(timeout=0.05)
            except subprocess.TimeoutExpired:
                pass
            if cancel is not None and cancel():
                raise BackendError(ErrorCode.CANCELLED, "Render cancelled")
            if time.monotonic() - started > timeout:
                raise BackendError(ErrorCode.TIMEOUT, f"OpenSCAD took longer than {timeout:g} s; try Draft quality")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _elapsed(started: float) -> int:
    return round((time.monotonic() - started) * 1000)
