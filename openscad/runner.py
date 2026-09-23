"""Safe OpenSCAD discovery, probing, and bounded subprocess rendering."""
from __future__ import annotations

import math
import os
import re
import shutil
import subprocess
import tempfile
import threading
from contextlib import suppress
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .cache import cache_key
from .catalog import QUALITY_PROFILES, defaults, source_path
from .errors import BackendError, ErrorCode
from .stl import mesh_stats, read_binary_stl
from .validation import validate_parameters

_VERSION_RE = re.compile(r"OpenSCAD version\s+([0-9]+(?:\.[0-9]+)+(?:[-+][0-9A-Za-z.-]+)?)", re.I)
_MIN_VERSION = (2023, 0)
CancelHook = Callable[[], bool] | threading.Event | None

@dataclass(frozen=True)
class EngineInfo:
    executable: str
    version: str | None
    supported: bool
    warning: str | None = None
    def to_dict(self) -> dict[str, object]:
        return {"executable": self.executable, "version": self.version, "supported": self.supported, "warning": self.warning}

@dataclass(frozen=True)
class RenderRequest:
    object_name: str
    params: dict[str, Any]
    output_path: Path
    quality_profile: str = "balanced"
    timeout: float = 120.0

@dataclass
class RenderResult:
    ok: bool
    output_path: str | None = None
    duration_ms: int = 0
    stats: dict[str, object] | None = None
    cache_key: str | None = None
    engine: dict[str, object] | None = None
    error: dict[str, object] | None = None
    argv: tuple[str, ...] = ()
    @property
    def output(self) -> Path | None:
        return Path(self.output_path) if self.output_path else None
    @property
    def elapsed_seconds(self) -> float:
        return self.duration_ms / 1000
    def to_dict(self) -> dict[str, object]:
        return {"ok": self.ok, "output_path": self.output_path, "duration_ms": self.duration_ms,
                "elapsed_seconds": self.elapsed_seconds, "stats": self.stats, "cache_key": self.cache_key,
                "engine": self.engine, "error": self.error, "argv": list(self.argv)}


def discover_openscad(executable: str | os.PathLike[str] | None = None) -> Path:
    candidate = str(executable) if executable is not None else shutil.which("openscad")
    if not candidate:
        raise BackendError(ErrorCode.NOT_FOUND, "OpenSCAD executable was not found")
    path = Path(candidate)
    if not path.is_absolute():
        path = Path(shutil.which(candidate) or candidate)
    path = path.resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise BackendError(ErrorCode.NOT_FOUND, "OpenSCAD executable is not executable", {"executable": str(path)})
    return path


def probe_openscad(executable: str | os.PathLike[str] | None = None, *, timeout: float = 5.0) -> EngineInfo:
    path = discover_openscad(executable)
    try:
        result = subprocess.run([str(path), "--version"], capture_output=True, text=True, check=False, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BackendError(ErrorCode.PROBE_FAILED, "OpenSCAD version probe failed", {"executable": str(path)}) from exc
    match = _VERSION_RE.search(f"{result.stdout}\n{result.stderr}")
    if result.returncode or not match:
        raise BackendError(ErrorCode.PROBE_FAILED, "OpenSCAD did not report a recognizable version", {"returncode": result.returncode})
    version = match.group(1)
    supported = tuple(int(part) for part in re.match(r"(\d+)\.(\d+)", version).groups()) >= _MIN_VERSION  # type: ignore[union-attr]
    warning = None if supported else f"OpenSCAD {version} is unsupported; the current upstream source requires OpenSCAD >=2023.0."
    return EngineInfo(str(path), version, supported, warning)


def discover(executable: str | os.PathLike[str] | None = None) -> Path:
    return discover_openscad(executable)


def probe(executable: str | os.PathLike[str] | None = None, timeout: float = 5.0) -> dict[str, object]:
    return probe_openscad(executable, timeout=timeout).to_dict()


def encode_define(name: str, value: Any) -> str:
    if not name or any(char in name for char in " \t\r\n=;\x00"):
        raise ValueError("unsafe OpenSCAD variable name")
    if type(value) is bool:
        encoded = "true" if value else "false"
    elif isinstance(value, int):
        encoded = str(value)
    elif isinstance(value, float) and math.isfinite(value):
        encoded = format(value, ".15g")
    else:
        raise ValueError("OpenSCAD -D values must be finite numbers or booleans")
    return f"{name}={encoded}"


def _scad_literal(value: Any) -> str:
    return encode_define("value", value).split("=", 1)[1]


def build_argv(executable: str | os.PathLike[str], *args: Any, quality_profile: str | None = None) -> list[str]:
    """Build argv; accepts object-oriented and source-path call forms."""
    if len(args) == 3 and isinstance(args[0], (str, os.PathLike)) and isinstance(args[1], dict):
        object_name, params, output = args
        source = source_path(str(object_name))
        quality = quality_profile or "balanced"
        checked = validate_parameters(str(object_name), params)
    elif len(args) == 4 and isinstance(args[1], dict):
        # Positional object form: object, params, output, quality.
        object_name, params, output, quality = args
        source = source_path(str(object_name))
        checked = validate_parameters(str(object_name), params)
        quality = str(quality)
    elif len(args) == 4:
        source, output, params, quality = args
        if not isinstance(params, dict):
            raise ValueError("parameters must be a mapping")
        quality = str(quality)
        checked = dict(params)
    else:
        raise TypeError("build_argv expects (executable, object, params, output[, quality]) or (executable, source, output, params, quality)")
    if quality not in QUALITY_PROFILES:
        raise BackendError(ErrorCode.INVALID_VALUE, "unknown quality profile", {"quality": quality})
    source = Path(source)
    if not source.is_file():
        raise BackendError(ErrorCode.SOURCE_NOT_FOUND, "OpenSCAD entry file is missing", {"source": str(source)})
    # OpenSCAD defaults to ASCII STL for some builds; the backend transports
    # binary STL and must request that format explicitly.
    argv = [str(executable), "-o", str(output), "--export-format", "binstl"]
    for name, value in checked.items():
        if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name):
            raise BackendError(ErrorCode.INVALID_VALUE, "invalid OpenSCAD variable name", {"parameter": name})
        argv.extend(("-D", encode_define(name, value)))
    profile = QUALITY_PROFILES[quality]
    argv.extend(("-D", encode_define("$fa", profile["fa"]), "-D", encode_define("$fs", profile["fs"]), str(source)))
    return argv


def _is_cancelled(cancel: CancelHook) -> bool:
    if cancel is None:
        return False
    return bool(cancel.is_set()) if hasattr(cancel, "is_set") else bool(cancel())  # type: ignore[operator]


class OpenSCADRunner:
    """Bounded synchronous runner; callers can discard stale results.

    ``cache_dir`` is opt-in so library users control where rendered artifacts
    are persisted. Cache entries are content-addressed by source revision,
    engine version, object parameters, and quality profile.
    """
    def __init__(self, executable: str | os.PathLike[str] | None = None, *,
                 probe: bool = True, cache_dir: str | os.PathLike[str] | None = None,
                 cache_max_bytes: int = 256 * 1024 * 1024):
        self.executable = discover_openscad(executable)
        self.engine = probe_openscad(self.executable) if probe else None
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.cache_max_bytes = max(0, int(cache_max_bytes))
        self._process: subprocess.Popen[str] | None = None
        self._generation = 0
        self._lock = threading.Lock()

    def cancel(self) -> None:
        with self._lock:
            self._generation += 1
            process = self._process
        if process and process.poll() is None:
            process.kill()

    def render_latest(self, request: RenderRequest, *, cancel: CancelHook = None) -> RenderResult:
        self.cancel()
        return self.render(request, cancel=cancel)

    def render(self, request: RenderRequest | str, params: dict[str, Any] | None = None,
               quality: str = "balanced", output: str | os.PathLike[str] | None = None,
               timeout: float = 120.0, cancel: CancelHook = None, cwd: str | os.PathLike[str] | None = None) -> RenderResult:
        if isinstance(request, RenderRequest):
            job = request
        else:
            if output is None:
                fd, generated_output = tempfile.mkstemp(suffix=".stl")
                os.close(fd)
                output = generated_output
            job = RenderRequest(request, params or defaults(request), Path(output), quality, timeout)
        started = time.monotonic()
        temp: Path | None = None
        process: subprocess.Popen[str] | None = None
        argv: list[str] = []
        with self._lock:
            generation = self._generation

        def superseded() -> bool:
            with self._lock:
                return generation != self._generation

        try:
            checked = validate_parameters(job.object_name, job.params)
            if job.timeout <= 0 or not math.isfinite(job.timeout):
                raise BackendError(ErrorCode.INVALID_VALUE, "render timeout must be positive and finite")
            if job.quality_profile not in QUALITY_PROFILES:
                raise BackendError(ErrorCode.INVALID_VALUE, "unknown quality profile", {"quality": job.quality_profile})
            if self.engine is None:
                self.engine = probe_openscad(self.executable)
            if not self.engine.supported:
                raise BackendError(ErrorCode.UNSUPPORTED_VERSION, self.engine.warning or "unsupported OpenSCAD version", self.engine.to_dict())
            destination = Path(job.output_path)
            destination.parent.mkdir(parents=True, exist_ok=True)
            identity = cache_key(job.object_name, checked, self.engine.version or "unknown", job.quality_profile)
            if self.cache_dir is not None:
                cache_path = self.cache_dir / f"{identity}.stl"
                try:
                    cached_mesh = read_binary_stl(cache_path) if cache_path.is_file() else None
                    if cached_mesh is not None:
                        if _is_cancelled(cancel) or superseded():
                            raise BackendError(ErrorCode.CANCELLED, "OpenSCAD render cancelled")
                        if cache_path.resolve() != destination.resolve():
                            shutil.copyfile(cache_path, destination)
                        return RenderResult(True, str(destination), round((time.monotonic() - started) * 1000),
                                            mesh_stats(cached_mesh), identity, self.engine.to_dict(), None, ())
                except BackendError:
                    cache_path.unlink(missing_ok=True)
                except OSError:
                    cache_path.unlink(missing_ok=True)
            handle, name = tempfile.mkstemp(prefix=".openscad-", suffix=".stl", dir=destination.parent)
            os.close(handle)
            temp = Path(name)
            argv = build_argv(self.executable, job.object_name, checked, temp, quality_profile=job.quality_profile)
            if _is_cancelled(cancel) or superseded():
                raise BackendError(ErrorCode.CANCELLED, "OpenSCAD render cancelled")
            source = Path(argv[-1])
            # OpenSCAD may emit one line per geometry echo; discard its pipes so
            # a noisy model cannot deadlock the bounded worker on a full buffer.
            process = subprocess.Popen(argv, cwd=cwd or source.parent, stdout=subprocess.DEVNULL,
                                       stderr=subprocess.DEVNULL, text=True, shell=False)
            with self._lock:
                self._process = process
            while process.poll() is None:
                if _is_cancelled(cancel) or superseded():
                    process.kill(); process.wait()
                    raise BackendError(ErrorCode.CANCELLED, "OpenSCAD render cancelled")
                if time.monotonic() - started > job.timeout:
                    process.kill(); process.wait()
                    raise BackendError(ErrorCode.TIMEOUT, "OpenSCAD render timed out", {"timeout": job.timeout})
                try:
                    process.wait(timeout=0.05)
                except subprocess.TimeoutExpired:
                    pass
            _, stderr = process.communicate()
            if _is_cancelled(cancel) or superseded():
                raise BackendError(ErrorCode.CANCELLED, "OpenSCAD render cancelled")
            if process.returncode != 0:
                raise BackendError(ErrorCode.PROCESS_FAILED, "OpenSCAD render failed", {"returncode": process.returncode, "stderr": (stderr or "")[-2000:]})
            if not temp.is_file() or temp.stat().st_size == 0:
                raise BackendError(ErrorCode.OUTPUT_ERROR, "OpenSCAD did not produce an STL")
            mesh = read_binary_stl(temp)
            if self.cache_dir is not None and self.cache_max_bytes and temp.stat().st_size <= self.cache_max_bytes:
                cache_temp: Path | None = None
                try:
                    self.cache_dir.mkdir(parents=True, exist_ok=True)
                    cache_path = self.cache_dir / f"{identity}.stl"
                    cache_temp = self.cache_dir / f".{identity}.stl.tmp-{os.getpid()}-{threading.get_ident()}"
                    shutil.copyfile(temp, cache_temp)
                    os.replace(cache_temp, cache_path)
                except OSError:
                    if cache_temp is not None:
                        with suppress(FileNotFoundError):
                            cache_temp.unlink()
            os.replace(temp, destination); temp = None
            return RenderResult(True, str(destination), round((time.monotonic() - started) * 1000), mesh_stats(mesh),
                                identity, self.engine.to_dict(), None, tuple(argv))
        except BackendError as exc:
            return RenderResult(False, duration_ms=round((time.monotonic() - started) * 1000), engine=self.engine.to_dict() if self.engine else None, error=exc.to_dict(), argv=tuple(argv))
        except (OSError, ValueError) as exc:
            error = BackendError(ErrorCode.OUTPUT_ERROR, str(exc))
            return RenderResult(False, duration_ms=round((time.monotonic() - started) * 1000), engine=self.engine.to_dict() if self.engine else None, error=error.to_dict(), argv=tuple(argv))
        finally:
            with self._lock:
                if self._process is process:
                    self._process = None
            if temp:
                temp.unlink(missing_ok=True)


def compatibility_smoke_test(executable: str | os.PathLike[str] | None = None, *, object_name: str = "bin", timeout: float = 30.0) -> dict[str, object]:
    with tempfile.TemporaryDirectory(prefix="openscad-smoke-") as directory:
        result = OpenSCADRunner(executable).render(RenderRequest(object_name, defaults(object_name), Path(directory) / "smoke.stl", "draft", timeout))
        return result.to_dict()
