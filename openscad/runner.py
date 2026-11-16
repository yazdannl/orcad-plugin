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

from .catalog import LIBRARY_REVISION, LIBRARY_SEARCH_PATH, QUALITY_PROFILES, source_path
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
Cancel = Callable[[], bool] | None
_UNIX_PATH = ":".join(("/usr/local/bin", "/opt/homebrew/bin", "/usr/bin", "/bin", "/usr/sbin", "/sbin"))


# Opened once at import, where no plugin audit scope is active. `subprocess.DEVNULL`
# opens the device for every spawn, and an audited open of /dev/null from a worker
# thread is denied and posts a permission dialog of its own.
DEVNULL_IN = open(os.devnull, "rb")
DEVNULL_OUT = open(os.devnull, "wb")


def _windows_directory() -> str:
    """The Windows directory without reading the environment; programs need it."""
    try:
        import ctypes

        buffer = ctypes.create_unicode_buffer(260)
        if ctypes.windll.kernel32.GetWindowsDirectoryW(buffer, len(buffer)):
            return buffer.value
    except Exception:
        pass
    return "C:\\Windows"


def child_env() -> dict[str, str]:
    """The environment for every program the plugin starts.

    Built from scratch instead of filtered from `os.environ`: a credential, a
    token or a shell setting exported in OrcaSlicer's process has no business
    in a renderer, and the host also carries an AppImage's LD_LIBRARY_PATH and
    LD_PRELOAD, which break an unrelated OpenSCAD. OPENSCADPATH is the one
    variable the vendored OpenSCAD libraries need.
    """
    home = Path.home()
    if os.name == "nt":
        root = Path(_windows_directory())
        # A fixed path, not tempfile.gettempdir(): that helper creates a probe
        # file in every candidate directory, and an audited create outside the
        # plugin's own data directory is denied (and pops a dialog) whenever a
        # worker thread asks.
        temp = str(home / "AppData" / "Local" / "Temp")
        env = {
            "SystemRoot": str(root), "WINDIR": str(root), "SYSTEMDRIVE": root.drive or "C:",
            "COMSPEC": str(root / "System32" / "cmd.exe"), "PATHEXT": ".COM;.EXE;.BAT;.CMD",
            "PATH": os.pathsep.join((str(root / "System32"), str(root), str(root / "System32" / "Wbem"))),
            "USERPROFILE": str(home), "HOMEDRIVE": home.drive or "C:",
            "HOMEPATH": str(home)[len(home.drive):] or "\\",
            "APPDATA": str(home / "AppData" / "Roaming"), "LOCALAPPDATA": str(home / "AppData" / "Local"),
            "TEMP": temp, "TMP": temp,
        }
    else:
        env = {"HOME": str(home), "TMPDIR": "/tmp", "PATH": _UNIX_PATH}
    # OpenSCAD separates library roots with os.pathsep (';' on Windows).
    env["OPENSCADPATH"] = os.pathsep.join(str(path) for path in LIBRARY_SEARCH_PATH)
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


def platform_name(system: str | None = None) -> str:
    system = system or sys.platform
    return "linux" if system.startswith("linux") else "win32" if system in ("win32", "windows") else system


# Apps launched from the macOS Dock or Finder do not inherit the shell PATH, so
# a Homebrew or drag-installed OpenSCAD is otherwise invisible to the plugin.
# These are trusted by location only, unlike the hash-verified download, so a
# candidate that another user owns or can write to is skipped.
_COMMON_LOCATIONS = {
    "darwin": ("/opt/homebrew/bin/openscad", "/usr/local/bin/openscad",
               "/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD",
               "~/Applications/OpenSCAD.app/Contents/MacOS/OpenSCAD"),
    "linux": ("/usr/local/bin/openscad", "/usr/bin/openscad", "/snap/bin/openscad"),
    # OrcaSlicer launched from the desktop does not inherit the shell PATH, so
    # the normal Windows installer location is invisible to shutil.which().
    "win32": (r"C:\Program Files\OpenSCAD\openscad.exe",
              r"C:\Program Files (x86)\OpenSCAD\openscad.exe",
              r"~/AppData/Local/Programs/OpenSCAD/openscad.exe"),
}


def _find_on_system() -> str | None:
    found = shutil.which("openscad")
    if found:
        return found
    for location in _COMMON_LOCATIONS.get(platform_name(), ()):
        path = os.path.expanduser(location)
        if not (os.path.isfile(path) and os.access(path, os.X_OK)):
            continue
        if os.name != "nt":
            st = os.stat(path)
            if st.st_uid not in (0, os.getuid()) or st.st_mode & 0o022:
                continue
        return path
    return None


def discover_openscad(executable: str | os.PathLike[str] | None = None) -> Path:
    if executable is not None:
        candidate = shutil.which(str(executable)) or str(executable)
    else:
        candidate = _find_on_system()
    if not candidate:
        raise BackendError(ErrorCode.NOT_FOUND, "OpenSCAD executable was not found")
    path = Path(candidate).resolve()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise BackendError(ErrorCode.NOT_FOUND, "OpenSCAD executable is not runnable", {"executable": str(path)})
    return path


def version_tuple(version: str) -> tuple[int, int]:
    match = re.match(r"(\d+)\.(\d+)", version)
    return (int(match.group(1)), int(match.group(2))) if match else (0, 0)


def _run_probe(argv: list[str], timeout: float):
    try:
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout,
                              env=child_env(), **popen_flags())
    except OSError:
        if os.name != "nt":  # a missing piece of the built environment is Windows-only;
            raise            # inheriting everything is still better than a dead plugin there
        return subprocess.run(argv, capture_output=True, text=True, check=False, timeout=timeout,
                              env=None, **popen_flags())


def probe_openscad(executable: str | os.PathLike[str] | None = None, *, timeout: float = 15.0) -> EngineInfo:
    """Run `--version` once, surviving the plugin audit hook's first crack at it.

    OrcaSlicer asks the user before a plugin starts a process. On a worker thread
    it denies the call that found the dialog still open and only then saves the
    grant, so the first launch of OpenSCAD comes back as PermissionError while
    the answer the user gave was Yes. Trying once more after the dialog had time
    to close turns that into the silent start it should have been.
    """
    path = discover_openscad(executable)
    for attempt in (0, 1):
        try:
            result = _run_probe([str(path), "--version"], timeout)
            break
        except PermissionError as exc:
            if attempt:
                raise BackendError(ErrorCode.PROBE_FAILED,
                                   "OrcaSlicer did not allow OpenSCAD to start. Allow it in the "
                                   "permission dialog, then try again.",
                                   {"executable": str(path)}) from exc
            time.sleep(1.0)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise BackendError(ErrorCode.PROBE_FAILED, "OpenSCAD could not be started",
                               {"executable": str(path)}) from exc
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
    elif type(value) is str:
        if len(value) > 2000:
            raise ValueError("OpenSCAD -D string values must be short")
        encoded = '"' + value.replace("\\", "\\\\").replace('"', '\\"') \
            .replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t") + '"'
    else:
        raise ValueError("OpenSCAD -D values must be finite numbers, booleans or strings")
    return f"{name}={encoded}"


def backend_args(version: str | None) -> list[str]:
    """Prefer the Manifold kernel and enable the text metrics the nameplate measures with."""
    year = version_tuple(version or "")[0]
    flags = ["--enable=textmetrics"] if year >= 2023 else []
    if year >= 2024:
        return [*flags, "--backend=Manifold"]
    return [*flags, "--enable=manifold"] if year == 2023 else flags


def build_argv(executable: str | os.PathLike[str], program: Path, output: Path,
               version: str | None) -> list[str]:
    """The OpenSCAD command line for one install.

    Nothing that varies per render may appear here. OrcaSlicer's plugin audit
    hook asks the user before plugin code starts a process and remembers the
    answer per exact command string, so a command line carrying the parameters
    or a per-render file name asks again on every single change. The parameters
    and the source therefore live in the program file (see `program`), and this
    argv stays byte-identical for every model, every parameter change and every
    restart.
    """
    return [str(executable), "-o", str(output), "--export-format", "binstl",
            *backend_args(version), str(program)]


def program(source: Path | None, defines: dict[str, Any], quality: str) -> str:
    """The OpenSCAD source for one render.

    Parameter values are assignments at the end of the top-level scope rather
    than -D arguments: OpenSCAD applies the last assignment to a name, which is
    the same rule that makes -D override the file, so a parameter still wins
    over the object's own default. A catalog wrapper is pulled in with an
    absolute `include` so its relative `use`/`include` paths keep resolving.
    """
    if quality not in QUALITY_PROFILES:
        raise BackendError(ErrorCode.INVALID_VALUE, f"Unknown quality profile {quality!r}")
    profile = QUALITY_PROFILES[quality]
    head = ""
    if source is not None:
        location = source.as_posix()
        if any(character in location for character in '<>"\n\r'):
            raise BackendError(ErrorCode.OUTPUT_ERROR, "The model source path cannot be used by OpenSCAD",
                               {"source": str(source)})
        head = f"include <{location}>\n"
    return head + "".join(f"{encode_define(name, value)};\n"
                          for name, value in {**defines, "$fa": profile["fa"],
                                              "$fs": profile["fs"]}.items())


def clean_log(text: str, *sources: Path) -> list[str]:
    lines = []
    for line in text.splitlines():
        if not line.strip() or line.startswith(_NOISE):
            continue
        for source in sources:
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
    """Serial renderer with a size-bounded, content-addressed STL cache.

    Every render runs the same command line over the same two files in `work/`.
    OrcaSlicer's plugin audit hook remembers a process grant per exact command
    string, so a per-render source or output name would ask the user for
    permission again on every change; see `build_argv`.
    """

    PROGRAM = "model.scad"
    OUTPUT = "out.stl"

    def __init__(self, executable: str | os.PathLike[str] | None = None, *,
                 cache_dir: str | os.PathLike[str], cache_limit: int = 512 * 1024 * 1024,
                 version: str | None = None):
        """Open a renderer.

        `version` is the version a verified install recorded when it unpacked,
        and passing it is what keeps a session to one audited command line: the
        `--version` probe is a second, different command, and OrcaSlicer asks the
        user about each distinct command once. Without it (an OpenSCAD the user
        installed) the probe runs, which is then the only extra prompt.
        """
        if version is None:
            self.engine = probe_openscad(executable)
        else:
            path = discover_openscad(executable)
            supported = version_tuple(version) >= MIN_VERSION
            self.engine = EngineInfo(str(path), version, supported, None if supported else
                                     f"OpenSCAD {version} is too old; version 2023 or newer is required.")
        if not self.engine.supported:
            raise BackendError(ErrorCode.UNSUPPORTED_VERSION, self.engine.warning or "unsupported OpenSCAD",
                               self.engine.to_dict())
        self.cache_dir = Path(cache_dir)
        self.cache_limit = cache_limit
        # Fixed for the life of the install, not per render: renders are serial in
        # one worker, so one shared pair of files cannot collide there.
        # ponytail: two OrcaSlicer windows sharing this directory that render at
        # the same instant can interleave the work files; per-process names would
        # cost a permission prompt after every start. Add an exclusive lock here
        # if the plugin ever has to support that.
        self.work = self.cache_dir / "work"

    def render_object(self, name: str, params: dict[str, Any], quality: str = "balanced", *,
                      timeout: float = 180.0, cancel: Cancel = None) -> RenderResult:
        checked = validate_parameters(name, params)
        source = source_path(name)
        identity = {"object": name, "params": checked}
        return self._render(program(source, checked, quality), quality, identity, timeout, cancel,
                            scrub=(source,))

    def render_code(self, code: str, quality: str = "balanced", *,
                    timeout: float = 180.0, cancel: Cancel = None) -> RenderResult:
        if not isinstance(code, str) or not code.strip():
            raise BackendError(ErrorCode.INVALID_VALUE, "The code is empty")
        if len(code) > MAX_CODE_CHARS:
            raise BackendError(ErrorCode.INVALID_VALUE, "The code is too large (over 200,000 characters)")
        return self._render(code + "\n" + program(None, {}, quality), quality, {"code": code}, timeout, cancel)

    def _render(self, text: str, quality: str, identity: dict[str, Any], timeout: float, cancel: Cancel,
                scrub: tuple[Path, ...] = ()) -> RenderResult:
        started = time.monotonic()
        payload = {**identity, "quality": quality, "engine": self.engine.version, "library": LIBRARY_REVISION}
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

        source, output = self.work / self.PROGRAM, self.work / self.OUTPUT
        self.work.mkdir(parents=True, exist_ok=True)
        source.write_text(text, encoding="utf-8")
        output.unlink(missing_ok=True)
        argv = build_argv(self.engine.executable, source, output, self.engine.version)
        error_log = self.cache_dir / f"{key}.stderr"
        try:
            # A named file, not tempfile.TemporaryFile: the latter re-opens its
            # descriptor with io.open(fd), and a targetless `open` cannot be
            # matched against the plugin's allowed root, so it asks permission.
            with error_log.open("w+b") as stderr:
                returncode = _run(argv, self.work, stderr, timeout, cancel)
                stderr.seek(0)
                log = clean_log(stderr.read().decode("utf-8", "replace"), source, *scrub)
            if returncode != 0:
                raise _failure(log, returncode)
            if not output.is_file():
                raise BackendError(ErrorCode.OUTPUT_ERROR, "OpenSCAD did not produce an STL")
            triangles = triangle_count(output.read_bytes())
            os.replace(output, stl)
            log_path.write_text("\n".join(log), encoding="utf-8")
        finally:
            output.unlink(missing_ok=True)
            try:
                error_log.unlink(missing_ok=True)
            except OSError:  # Windows can still hold the handle of a process just killed
                pass
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
    try:
        process = subprocess.Popen(argv, cwd=cwd, stdin=DEVNULL_IN, stdout=stderr, stderr=stderr,
                                   env=child_env(), **popen_flags())
    except OSError:
        if os.name != "nt":
            raise
        process = subprocess.Popen(argv, cwd=cwd, stdin=DEVNULL_IN, stdout=stderr, stderr=stderr,
                                   env=None, **popen_flags())
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
