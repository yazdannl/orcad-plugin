"""Provision a verified OpenSCAD build into a per-user cache when needed.

OrcaSlicer runs on several operating systems and must never ask for admin
rights, so the plugin downloads an official OpenSCAD development snapshot
(stable 2021.01 is too old for the Gridfinity library) and verifies it against
a pinned SHA-256 digest. Snapshots are eventually deleted upstream; when the
pinned file is gone, the newest snapshot for the platform is used instead and
verified against the digest published beside it on the same HTTPS host.
"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, replace
from pathlib import Path, PurePosixPath
from typing import Any, Callable

from .errors import BackendError, ErrorCode
from .runner import DEVNULL_OUT, child_env, discover_openscad, platform_name, popen_flags, probe_openscad, version_tuple

BOOTSTRAP_VERSION = "2026.09.22"
MIN_VERSION = (2023, 0)
SNAPSHOT_INDEX = "https://files.openscad.org/snapshots/"
MAX_DOWNLOAD_BYTES = 250 * 1024 * 1024
MAX_EXTRACTED_BYTES = 800 * 1024 * 1024
NETWORK_TIMEOUT = 60.0
INSTALL_TIMEOUT = 300.0


@dataclass(frozen=True)
class OpenSCADArtifact:
    platform: str
    architecture: str
    filename: str
    url: str
    sha256: str
    kind: str  # appimage | zip | dmg
    executable_name: str
    version: str = BOOTSTRAP_VERSION
    pattern: str = ""  # newest-snapshot fallback; empty disables it


ARTIFACTS = {
    ("linux", "x86_64"): OpenSCADArtifact(
        "linux", "x86_64", "OpenSCAD-2026.09.22-x86_64.AppImage",
        SNAPSHOT_INDEX + "OpenSCAD-2026.09.22-x86_64.AppImage",
        "474f7803ffcc3fbfc958c1ae8e57c12d9b78bdd3ad1eed5dd3417c2b85c5ad5a",
        "appimage", "openscad", pattern=r"OpenSCAD-(\d{4}\.\d\d\.\d\d)(?:\.ai\d+)?-x86_64\.AppImage",
    ),
    ("linux", "aarch64"): OpenSCADArtifact(
        "linux", "aarch64", "OpenSCAD-2023.09.11.ai-aarch64.AppImage",
        SNAPSHOT_INDEX + "OpenSCAD-2023.09.11.ai-aarch64.AppImage",
        "84d7bb1c71e14b4e248a84fbe0a4b02f58bcbf5326f0ee81c8a4de3653a3b568",
        "appimage", "openscad", "2023.09.11",
        pattern=r"OpenSCAD-(\d{4}\.\d\d\.\d\d)(?:\.ai\d*)?-aarch64\.AppImage",
    ),
    ("darwin", "universal"): OpenSCADArtifact(
        "darwin", "universal", "OpenSCAD-2026.09.22.dmg",
        SNAPSHOT_INDEX + "OpenSCAD-2026.09.22.dmg",
        "eb64bc53525e6ce57a756ab7df5339b7f5493739147a9a4f02eae7ca3301ac13",
        "dmg", "OpenSCAD", pattern=r"OpenSCAD-(\d{4}\.\d\d\.\d\d)\.dmg",
    ),
    ("win32", "x86_64"): OpenSCADArtifact(
        "win32", "x86_64", "OpenSCAD-2026.09.22-x86-64.zip",
        SNAPSHOT_INDEX + "OpenSCAD-2026.09.22-x86-64.zip",
        "40328a7da0127b96d7a06fc9d8031e92f21b9ab32c3530509a0c0888afb556d8",
        "zip", "openscad.exe", pattern=r"OpenSCAD-(\d{4}\.\d\d\.\d\d)-x86-64\.zip",
    ),
}


def artifact_for(system: str | None = None, machine: str | None = None) -> OpenSCADArtifact:
    system = platform_name(system)
    arch = (machine or platform.machine()).lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(arch, arch)
    key = (system, "universal") if system == "darwin" else (system, arch)
    if key not in ARTIFACTS:
        raise BackendError(ErrorCode.UNSUPPORTED_PLATFORM,
                           f"Automatic OpenSCAD setup is unavailable for {system}/{arch}; "
                           "install OpenSCAD 2023 or newer and add it to PATH.",
                           {"platform": system, "architecture": arch})
    return ARTIFACTS[key]


def cache_root(root: str | os.PathLike[str] | None = None) -> Path:
    if root is not None:
        return Path(root).expanduser()
    # Derived from the user's home, never from the environment: the cache path
    # must not change with whatever OrcaSlicer happened to be started from.
    if sys.platform == "win32":
        base = Path.home() / "AppData" / "Local"
    elif sys.platform == "darwin":
        base = Path.home() / "Library" / "Caches"
    else:
        base = Path.home() / ".cache"
    return Path(base).expanduser() / "orcad" / "openscad"


# ---------------------------------------------------------------- download

def _fetch(url: str, opener: Callable[..., Any]):
    request = urllib.request.Request(url, headers={"User-Agent": "orcad-plugin"})
    response = opener(request, timeout=NETWORK_TIMEOUT)
    if not str(getattr(response, "geturl", lambda: url)()).startswith("https://"):
        response.close()
        raise ValueError("download was redirected to a non-HTTPS URL")
    return response


def _download_with_retry(artifact: OpenSCADArtifact, destination: Path, opener: Callable[..., Any]) -> None:
    """Retry a dropped connection once; never retry a checksum or HTTP status failure."""
    for attempt in (0, 1):
        try:
            _download(artifact, destination, opener)
            return
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt:
                raise
            time.sleep(1)


def newest_snapshot(artifact: OpenSCADArtifact, opener: Callable[..., Any] = urllib.request.urlopen) -> OpenSCADArtifact:
    """Resolve the newest upstream snapshot matching *artifact* plus its published digest."""
    with _fetch(SNAPSHOT_INDEX, opener) as response:
        index = response.read(8 * 1024 * 1024).decode("utf-8", "replace")
    found = {match.group(0): match.group(1) for match in re.finditer(artifact.pattern, index)}
    if not found:
        raise ValueError("no matching OpenSCAD snapshot is published")
    filename = max(found, key=lambda name: (found[name], name))
    with _fetch(SNAPSHOT_INDEX + filename + ".sha256", opener) as response:
        digest = response.read(4096).decode("ascii", "replace").split()[0].lower()
    if not re.fullmatch(r"[0-9a-f]{64}", digest):
        raise ValueError("the published snapshot digest is malformed")
    return replace(artifact, filename=filename, url=SNAPSHOT_INDEX + filename, sha256=digest, version=found[filename])


def _download(artifact: OpenSCADArtifact, destination: Path, opener: Callable[..., Any]) -> None:
    partial = destination.with_name(destination.name + ".part")
    digest, total = hashlib.sha256(), 0
    try:
        with _fetch(artifact.url, opener) as response, partial.open("wb") as stream:
            size = int(response.headers.get("Content-Length") or 0)
            if size > MAX_DOWNLOAD_BYTES:
                raise ValueError("the OpenSCAD download is unexpectedly large")
            while chunk := response.read(1024 * 1024):
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("the OpenSCAD download is unexpectedly large")
                digest.update(chunk)
                stream.write(chunk)
                _set_progress(total, size)
        if digest.hexdigest() != artifact.sha256:
            raise ValueError("the OpenSCAD download failed its checksum")
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


# ----------------------------------------------------------------- install

def _safe_member_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if "\\" in name or relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("the OpenSCAD archive contains an unsafe path")
    target = root.joinpath(*relative.parts).resolve()
    if target != root.resolve() and root.resolve() not in target.parents:
        raise ValueError("the OpenSCAD archive contains an unsafe path")
    return target


_AUDIT_DENIED_COMPONENTS = frozenset({
    "secret", "secrets", "cert", "certs", "certificate", "certificates", "conf", "config",
})


def _audit_denied_member(name: str) -> bool:
    parts = PurePosixPath(name).parts
    return (any(part.casefold() in _AUDIT_DENIED_COMPONENTS for part in parts)
            or parts[-1].casefold().endswith((".conf", ".ini")))


def _safe_extract_zip(archive_path: Path, destination: Path) -> None:
    extracted = 0
    with zipfile.ZipFile(archive_path) as archive:
        for info in archive.infolist():
            extracted += max(0, info.file_size)
            if extracted > MAX_EXTRACTED_BYTES:
                raise ValueError("the OpenSCAD archive is unexpectedly large")
            target = _safe_member_path(destination, info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("the OpenSCAD archive contains a symlink")
            # OrcaSlicer PluginAuditManager::default_denied_path_keywords and
            # has_denied_config_extension deny these names even under data_dir();
            # writing them in-process aborts setup on Windows. They are optional.
            if _audit_denied_member(info.filename):
                continue
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info) as source, target.open("wb") as stream:
                shutil.copyfileobj(source, stream)
            target.chmod(((info.external_attr >> 16) & 0o777) or 0o644)


def _find_executable(root: Path, name: str) -> Path:
    for path in sorted(root.rglob("*"), key=lambda item: len(item.parts)):
        if path.name.casefold() == name.casefold() and path.is_file() and not path.is_symlink():
            if os.name != "nt":
                path.chmod(path.stat().st_mode | stat.S_IXUSR)
            return path
    raise FileNotFoundError(f"{name} was not found in the OpenSCAD package")


def _run_quiet(argv: list[str], cwd: Path | None = None, *, allow_failure: bool = False) -> None:
    result = subprocess.run(argv, cwd=cwd, stdout=DEVNULL_OUT, stderr=DEVNULL_OUT,
                            check=False, timeout=INSTALL_TIMEOUT, env=child_env(), **popen_flags())
    if result.returncode and not allow_failure:
        raise RuntimeError(f"{Path(argv[0]).name} failed with exit code {result.returncode}")


def _install_appimage(archive: Path, destination: Path) -> Path:
    """Extract instead of mounting: AppImages need FUSE, which many systems lack."""
    image = destination / archive.name
    shutil.copyfile(archive, image)
    image.chmod(0o755)
    try:
        _run_quiet([str(image), "--appimage-extract"], cwd=destination)
        executable = _find_executable(destination / "squashfs-root" / "usr" / "bin", "openscad")
    except (OSError, RuntimeError, subprocess.TimeoutExpired):
        return image  # still works where FUSE is available
    image.unlink(missing_ok=True)
    return executable


def _install_dmg(archive: Path, destination: Path) -> Path:
    """Copy OpenSCAD.app out of the disk image.

    The mount point is fixed under the cache rather than a fresh temp directory:
    OrcaSlicer's plugin audit hook remembers a process grant per exact command
    string, and a random directory in the hdiutil command line would ask for the
    same permission on every install attempt.
    """
    mount = destination / "mnt"
    shutil.rmtree(mount, ignore_errors=True)
    mount.mkdir(parents=True)
    _run_quiet(["hdiutil", "attach", "-nobrowse", "-readonly", "-mountpoint", str(mount), str(archive)])
    try:
        app = next(iter(sorted(mount.glob("*.app"))), None)
        if app is None:
            raise FileNotFoundError("OpenSCAD.app was not found in the disk image")
        # ditto, not a copy: the bundle's signature and extended attributes are
        # what macOS checks before it runs the app.
        _run_quiet(["ditto", str(app), str(destination / app.name)])
    finally:
        _run_quiet(["hdiutil", "detach", str(mount), "-force"], allow_failure=True)
        shutil.rmtree(mount, ignore_errors=True)
    return _find_executable(destination, "OpenSCAD")


def _install(artifact: OpenSCADArtifact, archive: Path, destination: Path) -> tuple[Path, str]:
    if destination.exists():
        shutil.rmtree(destination)
    destination.mkdir(parents=True)
    if artifact.kind == "appimage":
        executable = _install_appimage(archive, destination)
    elif artifact.kind == "zip":
        _safe_extract_zip(archive, destination)
        executable = _find_executable(destination, artifact.executable_name)
    else:
        executable = _install_dmg(archive, destination)
    # The version comes from the pinned artifact, not from running the download:
    # the archive was verified against its SHA-256, and a `--version` probe is a
    # second command line that OrcaSlicer would ask the user about again.
    if version_tuple(artifact.version) < MIN_VERSION:
        raise ValueError(f"the downloaded OpenSCAD build {artifact.version} is unsupported")
    marker = {"version": artifact.version, "sha256": artifact.sha256,
              "executable": executable.relative_to(destination).as_posix()}
    (destination / "orcad-install.json").write_text(json.dumps(marker), encoding="utf-8")
    return executable, artifact.version


def installed_engine(root: str | os.PathLike[str] | None = None) -> tuple[Path, str] | None:
    """The OpenSCAD this plugin installed, without running anything.

    Reads the marker written at install time. It is what lets the plugin
    declare its exact render command while OrcaSlicer loads it, instead of
    meeting a permission prompt on the first render.
    """
    try:
        artifact = artifact_for()
    except BackendError:
        return None
    destination = cache_root(root) / f"{artifact.platform}-{artifact.architecture}"
    return _installed(destination)


def _installed(destination: Path) -> tuple[Path, str] | None:
    try:
        marker = json.loads((destination / "orcad-install.json").read_text(encoding="utf-8"))
        executable = _safe_member_path(destination, str(marker["executable"]))
        version = str(marker["version"])
    except (OSError, ValueError, KeyError, TypeError):
        return None
    if not executable.is_file() or version_tuple(version) < MIN_VERSION:
        return None
    return executable, version


def ensure_openscad(*, root: str | os.PathLike[str] | None = None, system: str | None = None,
                    machine: str | None = None, opener: Callable[..., Any] = urllib.request.urlopen) -> tuple[Path, str]:
    """Return a supported OpenSCAD executable and its version, installing one per user if needed."""
    try:
        existing = discover_openscad()
        if (info := probe_openscad(existing)).supported:
            return existing, info.version or ""
    except BackendError:
        pass
    artifact = artifact_for(system, machine)
    cache = cache_root(root)
    destination = cache / f"{artifact.platform}-{artifact.architecture}"
    with _INSTALL_LOCK:
        installed = _installed(destination)
        if installed is not None:
            return installed
        cache.mkdir(parents=True, exist_ok=True)
        try:
            try:
                archive = cache / artifact.filename
                _download_with_retry(artifact, archive, opener)
            except urllib.error.HTTPError as exc:
                if exc.code not in (403, 404, 410) or not artifact.pattern:
                    raise
                artifact = newest_snapshot(artifact, opener)
                archive = cache / artifact.filename
                _download_with_retry(artifact, archive, opener)
            try:
                return _install(artifact, archive, destination)
            finally:
                archive.unlink(missing_ok=True)
        except Exception as exc:
            shutil.rmtree(destination, ignore_errors=True)
            reason = str(exc)[:200]
            message = (f"Automatic OpenSCAD setup failed: {reason}. "
                       "Check the network connection and retry, or install OpenSCAD 2023 or newer "
                       "and add it to PATH.") if reason else (
                       "Automatic OpenSCAD setup failed. Check the network connection and retry, or install "
                       "OpenSCAD 2023 or newer and add it to PATH.")
            raise BackendError(
                ErrorCode.INSTALL_FAILED, message,
                {"cause": str(exc)[:500], "version": artifact.version},
            ) from exc


# ------------------------------------------------------- background state

_INSTALL_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_DONE = threading.Event()
_STATE: dict[str, Any] = {"state": "idle", "path": None, "version": None, "error": None, "progress": None}


def _set_progress(done: int, total: int) -> None:
    with _STATE_LOCK:
        _STATE["progress"] = round(done / total, 3) if total else None


def _worker(root: str | os.PathLike[str] | None) -> None:
    try:
        path, version = ensure_openscad(root=root)
        update = {"state": "ready", "path": str(path), "version": version, "error": None}
    except BackendError as exc:
        try:
            if root is not None:
                target = Path(root) / "setup-error.txt"
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_text(f"{exc.message}\n{exc.details}\n", encoding="utf-8")
        except OSError:
            pass
        update = {"state": "failed", "path": None, "version": None, "error": exc}
    except Exception as exc:  # pragma: no cover - defensive
        update = {"state": "failed", "path": None, "version": None,
                  "error": BackendError(ErrorCode.INSTALL_FAILED, str(exc)[:500])}
    with _STATE_LOCK:
        _STATE.update(update, progress=None)
    _DONE.set()


def start_openscad_bootstrap(root: str | os.PathLike[str] | None = None) -> None:
    """Start (or retry after a failure) the non-blocking setup; idempotent."""
    with _STATE_LOCK:
        if _STATE["state"] in ("starting", "ready"):
            return
        _STATE.update(state="starting", path=None, version=None, error=None, progress=None)
        _DONE.clear()
    threading.Thread(target=_worker, args=(root,), name="orcad-openscad-setup", daemon=True).start()


def openscad_bootstrap_status() -> dict[str, Any]:
    with _STATE_LOCK:
        error = _STATE["error"]
        return {"state": _STATE["state"], "path": _STATE["path"], "version": _STATE.get("version"),
                "progress": _STATE["progress"],
                "error": error.message if error else None,
                "error_details": error.details if error else None}


def wait_for_openscad(cancel: Callable[[], bool] | None = None, timeout: float | None = None,
                      root: str | os.PathLike[str] | None = None) -> Path:
    """Block until setup finishes (retrying a failed one), honouring *cancel*."""
    start_openscad_bootstrap(root)
    deadline = None if timeout is None else time.monotonic() + timeout
    while not _DONE.wait(0.1):
        if cancel is not None and cancel():
            raise BackendError(ErrorCode.CANCELLED, "OpenSCAD setup wait was cancelled")
        if deadline is not None and time.monotonic() > deadline:
            raise BackendError(ErrorCode.INSTALL_FAILED, "OpenSCAD setup is still running")
    with _STATE_LOCK:
        if _STATE["state"] == "ready":
            return Path(_STATE["path"])
        raise _STATE["error"] or BackendError(ErrorCode.INSTALL_FAILED, "OpenSCAD setup did not finish")
