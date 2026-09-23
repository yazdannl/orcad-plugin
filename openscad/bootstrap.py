"""Download a verified OpenSCAD build into a per-user cache when needed.

The plugin cannot rely on a system package manager because OrcaSlicer runs on
multiple operating systems and must not request administrator privileges. The
artifact table is intentionally pinned: a startup download is accepted only
from the official OpenSCAD snapshot URL with the checked-in SHA-256 digest.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .errors import BackendError, ErrorCode
from .runner import discover_openscad, probe_openscad

BOOTSTRAP_VERSION = "2026.09.22"
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024
MAX_EXTRACTED_BYTES = 500 * 1024 * 1024
DOWNLOAD_TIMEOUT_SECONDS = 120.0
PROBE_TIMEOUT_SECONDS = 10.0


@dataclass(frozen=True)
class OpenSCADArtifact:
    platform: str
    architecture: str
    filename: str
    url: str
    sha256: str
    kind: str
    executable_name: str
    version: str = BOOTSTRAP_VERSION


# Official OpenSCAD snapshot artifacts. Keep these immutable and update the
# digest whenever the pinned snapshot is intentionally changed.
ARTIFACTS = {
    ("linux", "x86_64"): OpenSCADArtifact(
        "linux", "x86_64", "OpenSCAD-2026.09.22-x86_64.AppImage",
        "https://files.openscad.org/snapshots/OpenSCAD-2026.09.22-x86_64.AppImage",
        "474f7803ffcc3fbfc958c1ae8e57c12d9b78bdd3ad1eed5dd3417c2b85c5ad5a",
        "appimage", "OpenSCAD-2026.09.22-x86_64.AppImage",
    ),
    ("linux", "aarch64"): OpenSCADArtifact(
        "linux", "aarch64", "OpenSCAD-2023.09.11.ai-aarch64.AppImage",
        "https://files.openscad.org/snapshots/OpenSCAD-2023.09.11.ai-aarch64.AppImage",
        "84d7bb1c71e14b4e248a84fbe0a4b02f58bcbf5326f0ee81c8a4de3653a3b568",
        "appimage", "OpenSCAD-2023.09.11.ai-aarch64.AppImage", "2023.09.11",
    ),
    ("darwin", "universal"): OpenSCADArtifact(
        "darwin", "universal", "OpenSCAD-2026.09.22.dmg",
        "https://files.openscad.org/snapshots/OpenSCAD-2026.09.22.dmg",
        "eb64bc53525e6ce57a756ab7df5339b7f5493739147a9a4f02eae7ca3301ac13",
        "dmg", "OpenSCAD",
    ),
    ("win32", "x86_64"): OpenSCADArtifact(
        "win32", "x86_64", "OpenSCAD-2026.09.22-x86-64.zip",
        "https://files.openscad.org/snapshots/OpenSCAD-2026.09.22-x86-64.zip",
        "40328a7da0127b96d7a06fc9d8031e92f21b9ab32c3530509a0c0888afb556d8",
        "zip", "openscad.exe",
    ),
}


def _normalized_system(system: str | None = None) -> str:
    value = system or sys.platform
    if value.startswith("linux"):
        return "linux"
    if value == "darwin":
        return "darwin"
    if value in ("win32", "windows"):
        return "win32"
    return value


def _normalized_architecture(machine: str | None = None) -> str:
    value = (machine or platform.machine()).lower()
    if value in ("x86_64", "amd64", "x64"):
        return "x86_64"
    if value in ("aarch64", "arm64"):
        return "aarch64"
    return value


def artifact_for(system: str | None = None, machine: str | None = None) -> OpenSCADArtifact:
    normalized_system = _normalized_system(system)
    architecture = _normalized_architecture(machine)
    key = (normalized_system, "universal") if normalized_system == "darwin" else (normalized_system, architecture)
    try:
        return ARTIFACTS[key]
    except KeyError as exc:
        label = f"{normalized_system}/{architecture}"
        raise BackendError(
            ErrorCode.UNSUPPORTED_PLATFORM,
            f"Automatic OpenSCAD installation is unavailable for {label}; install OpenSCAD >=2023 manually.",
            {"platform": normalized_system, "architecture": architecture},
        ) from exc


def cache_root(root: str | os.PathLike[str] | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser()
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = os.environ.get("XDG_CACHE_HOME") or (Path.home() / ".cache")
    return Path(base).expanduser() / "orcad" / "openscad"


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _safe_error(message: str, artifact: OpenSCADArtifact, *, cause: Exception | None = None) -> BackendError:
    details: dict[str, Any] = {
        "version": artifact.version,
        "platform": artifact.platform,
        "architecture": artifact.architecture,
    }
    if cause is not None:
        details["cause"] = str(cause)[:500]
    return BackendError(ErrorCode.INSTALL_FAILED, message, details)


def _download(
    artifact: OpenSCADArtifact,
    destination: Path,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    partial = destination.with_name(f".{destination.name}.part")
    partial.unlink(missing_ok=True)
    try:
        request = urllib.request.Request(
            artifact.url,
            headers={"User-Agent": "orcad-plugin/OpenSCAD-bootstrap"},
        )
        with opener(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            final_url = str(getattr(response, "geturl", lambda: artifact.url)())
            if not final_url.startswith("https://"):
                raise ValueError("OpenSCAD download was redirected to a non-HTTPS URL")
            content_length = response.headers.get("Content-Length")
            if content_length and int(content_length) > MAX_DOWNLOAD_BYTES:
                raise ValueError("OpenSCAD download exceeds the safety size limit")
            total = 0
            digest = hashlib.sha256()
            with partial.open("wb") as stream:
                while True:
                    chunk = response.read(1024 * 1024)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > MAX_DOWNLOAD_BYTES:
                        raise ValueError("OpenSCAD download exceeds the safety size limit")
                    digest.update(chunk)
                    stream.write(chunk)
        if digest.hexdigest() != artifact.sha256:
            raise ValueError("OpenSCAD download checksum did not match the pinned digest")
        os.replace(partial, destination)
        if os.name != "nt":
            destination.chmod(0o700)
    finally:
        partial.unlink(missing_ok=True)


def _safe_member_path(root: Path, name: str) -> Path:
    if "\\" in name:
        raise ValueError("OpenSCAD archive contains an unsafe path")
    relative = PurePosixPath(name)
    if relative.is_absolute() or not relative.parts or ".." in relative.parts:
        raise ValueError("OpenSCAD archive contains an unsafe path")
    target = (root.joinpath(*relative.parts)).resolve()
    resolved_root = root.resolve()
    if target != resolved_root and resolved_root not in target.parents:
        raise ValueError("OpenSCAD archive contains an unsafe path")
    return target


def _safe_extract_zip(archive_path: Path, destination: Path) -> None:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    extracted_bytes = 0
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            extracted_bytes += max(0, info.file_size)
            if extracted_bytes > MAX_EXTRACTED_BYTES:
                raise ValueError("OpenSCAD archive exceeds the extracted size limit")
            target = _safe_member_path(destination, info.filename)
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise ValueError("OpenSCAD archive contains a symlink")
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("wb") as stream:
                stream.write(archive.read(info))
            permissions = (info.external_attr >> 16) & 0o777
            target.chmod(permissions or 0o600)


def _find_executable(root: Path, executable_name: str) -> Path:
    wanted = executable_name.casefold()
    candidates = sorted(
        path for path in root.rglob("*")
        if path.is_file() and not path.is_symlink() and path.name.casefold() == wanted
    )
    if not candidates:
        raise FileNotFoundError(f"{executable_name} was not found in the OpenSCAD package")
    candidate = candidates[0]
    if os.name != "nt":
        candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
    return candidate


def _install_dmg(archive_path: Path, destination: Path) -> Path:
    destination.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.TemporaryDirectory(prefix="orcad-openscad-mount-") as directory:
        mount = Path(directory)
        attach = subprocess.run(
            ["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(archive_path)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
            timeout=DOWNLOAD_TIMEOUT_SECONDS,
        )
        if attach.returncode:
            raise RuntimeError("hdiutil could not mount the OpenSCAD disk image")
        try:
            apps = sorted(path for path in mount.glob("*.app") if path.is_dir())
            if not apps:
                raise FileNotFoundError("OpenSCAD.app was not found in the disk image")
            app = next((path for path in apps if path.name == "OpenSCAD.app"), apps[0])
            target = destination / app.name
            copied = subprocess.run(
                ["ditto", str(app), str(target)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
            if copied.returncode:
                raise RuntimeError("ditto could not install the OpenSCAD application")
        finally:
            detached = subprocess.run(
                ["hdiutil", "detach", str(mount), "-force"],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                timeout=DOWNLOAD_TIMEOUT_SECONDS,
            )
            if detached.returncode:
                raise RuntimeError("hdiutil could not detach the OpenSCAD disk image")
    return _find_executable(destination, "OpenSCAD")


def _marker_path(install_dir: Path) -> Path:
    return install_dir / ".orcad-openscad.json"


def _read_cached_candidate(install_dir: Path, artifact: OpenSCADArtifact) -> Path | None:
    marker = _marker_path(install_dir)
    try:
        data = json.loads(marker.read_text(encoding="utf-8"))
        if data.get("artifact_sha256") != artifact.sha256:
            return None
        relative = PurePosixPath(str(data["candidate"]))
        candidate = _safe_member_path(install_dir, relative.as_posix())
        if not candidate.is_file() or _sha256(candidate) != data.get("executable_sha256"):
            return None
        probe_openscad(candidate, timeout=PROBE_TIMEOUT_SECONDS)
        return candidate
    except (BackendError, OSError, ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None


def _write_marker(install_dir: Path, artifact: OpenSCADArtifact, candidate: Path) -> None:
    marker = _marker_path(install_dir)
    temporary = marker.with_name(f".{marker.name}.tmp")
    data = {
        "artifact_sha256": artifact.sha256,
        "candidate": candidate.relative_to(install_dir).as_posix(),
        "executable_sha256": _sha256(candidate),
    }
    temporary.write_text(json.dumps(data, sort_keys=True) + "\n", encoding="utf-8")
    os.replace(temporary, marker)


def _install_artifact(artifact: OpenSCADArtifact, artifact_path: Path, cache: Path) -> Path:
    base = f"{artifact.version}-{artifact.platform}-{artifact.architecture}-{artifact.sha256[:12]}"
    for attempt in range(100):
        name = base if attempt == 0 else f"{base}-{attempt}"
        install_dir = cache / name
        cached = _read_cached_candidate(install_dir, artifact)
        if cached is not None:
            return cached
        if install_dir.exists():
            continue
        try:
            install_dir.mkdir(parents=True, mode=0o700)
        except FileExistsError:
            continue
        if artifact.kind == "appimage":
            candidate = install_dir / artifact.executable_name
            shutil.copyfile(artifact_path, candidate)
            candidate.chmod(candidate.stat().st_mode | stat.S_IXUSR)
        elif artifact.kind == "zip":
            _safe_extract_zip(artifact_path, install_dir)
            candidate = _find_executable(install_dir, artifact.executable_name)
        elif artifact.kind == "dmg":
            candidate = _install_dmg(artifact_path, install_dir)
        else:  # pragma: no cover - the checked-in table is exhaustive
            raise ValueError(f"unknown OpenSCAD artifact kind: {artifact.kind}")
        info = probe_openscad(candidate, timeout=PROBE_TIMEOUT_SECONDS)
        if not info.supported:
            raise ValueError(info.warning or "the downloaded OpenSCAD build is unsupported")
        _write_marker(install_dir, artifact, candidate)
        return candidate
    raise RuntimeError("could not allocate a clean OpenSCAD installation directory")


def ensure_openscad(
    *,
    root: str | os.PathLike[str] | None = None,
    system: str | None = None,
    machine: str | None = None,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> Path:
    """Return a supported executable, downloading the pinned build if needed."""
    try:
        existing = discover_openscad()
        info = probe_openscad(existing, timeout=PROBE_TIMEOUT_SECONDS)
        if info.supported:
            return existing
    except (BackendError, OSError):
        pass

    artifact = artifact_for(system, machine)
    cache = cache_root(root)
    cache.mkdir(parents=True, exist_ok=True)
    if os.name != "nt":
        cache.chmod(0o700)
    artifact_path = cache / artifact.filename
    with _INSTALL_LOCK:
        if artifact_path.is_symlink():
            artifact_path.unlink(missing_ok=True)
        if artifact_path.is_file():
            try:
                valid_artifact = _sha256(artifact_path) == artifact.sha256
            except OSError:
                valid_artifact = False
            if not valid_artifact:
                artifact_path.unlink(missing_ok=True)
        if not artifact_path.is_file():
            try:
                _download(artifact, artifact_path, opener=opener)
            except Exception as exc:
                raise _safe_error(
                    "Automatic OpenSCAD installation failed; install OpenSCAD >=2023 manually and retry.",
                    artifact,
                    cause=exc,
                ) from exc
        try:
            candidate = _install_artifact(artifact, artifact_path, cache)
            return candidate
        except Exception as exc:
            raise _safe_error(
                "Automatic OpenSCAD installation failed; install OpenSCAD >=2023 manually and retry.",
                artifact,
                cause=exc,
            ) from exc


_INSTALL_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_STATE_EVENT = threading.Event()
_STATE = "idle"
_STATE_PATH: Path | None = None
_STATE_ERROR: BackendError | None = None


def _bootstrap_worker(root: str | os.PathLike[str] | None) -> None:
    global _STATE, _STATE_PATH, _STATE_ERROR
    try:
        path = ensure_openscad(root=root)
    except BackendError as exc:
        with _STATE_LOCK:
            _STATE = "failed"
            _STATE_ERROR = exc
    except Exception as exc:  # pragma: no cover - defensive conversion
        with _STATE_LOCK:
            _STATE = "failed"
            _STATE_ERROR = BackendError(ErrorCode.INSTALL_FAILED, str(exc)[:500])
    else:
        with _STATE_LOCK:
            _STATE = "ready"
            _STATE_PATH = path
            _STATE_ERROR = None
    finally:
        _STATE_EVENT.set()


def start_openscad_bootstrap(root: str | os.PathLike[str] | None = None) -> None:
    """Start the non-blocking startup installer once for this plugin process."""
    global _STATE, _STATE_PATH, _STATE_ERROR
    with _STATE_LOCK:
        if _STATE in ("starting", "ready"):
            return
        _STATE = "starting"
        _STATE_PATH = None
        _STATE_ERROR = None
        _STATE_EVENT.clear()
        threading.Thread(
            target=_bootstrap_worker, args=(root,), name="orcad-openscad-bootstrap", daemon=True
        ).start()


def openscad_bootstrap_status() -> dict[str, str | None]:
    with _STATE_LOCK:
        return {
            "state": _STATE,
            "path": str(_STATE_PATH) if _STATE_PATH else None,
            "error": str(_STATE_ERROR) if _STATE_ERROR else None,
        }


def wait_for_openscad(
    timeout: float = DOWNLOAD_TIMEOUT_SECONDS + PROBE_TIMEOUT_SECONDS,
    root: str | os.PathLike[str] | None = None,
    cancel: Callable[[], bool] | None = None,
) -> Path:
    """Wait for startup provisioning, retrying after a previous failed attempt."""
    start_openscad_bootstrap(root)
    deadline = time.monotonic() + max(0.0, float(timeout))
    while not _STATE_EVENT.is_set():
        if cancel is not None and cancel():
            raise BackendError(ErrorCode.CANCELLED, "OpenSCAD setup was cancelled")
        remaining = deadline - time.monotonic()
        if remaining <= 0 or _STATE_EVENT.wait(min(0.25, remaining)):
            break
    if not _STATE_EVENT.is_set():
        raise BackendError(
            ErrorCode.INSTALL_FAILED,
            "OpenSCAD setup is still in progress; retry the preview after startup finishes.",
        )
    with _STATE_LOCK:
        if _STATE_PATH is not None and _STATE == "ready":
            return _STATE_PATH
        if _STATE_ERROR is not None:
            error = _STATE_ERROR
        else:
            error = BackendError(ErrorCode.INSTALL_FAILED, "OpenSCAD setup did not produce an executable")
    raise error


__all__ = [
    "ARTIFACTS", "BOOTSTRAP_VERSION", "OpenSCADArtifact", "artifact_for", "cache_root",
    "ensure_openscad", "openscad_bootstrap_status", "start_openscad_bootstrap", "wait_for_openscad",
]
