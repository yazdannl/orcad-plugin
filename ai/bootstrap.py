"""Install the pinned Node.js runtime and Pi under the orcad AI cache."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import subprocess
import sys
import tarfile
import threading
import time
import urllib.request
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Callable

try:
    from orcad_openscad.bootstrap import cache_root as openscad_cache_root
    from orcad_openscad.runner import child_env, popen_flags
except ImportError:
    from openscad.bootstrap import cache_root as openscad_cache_root
    from openscad.runner import child_env, popen_flags

NODE_VERSION = "24.21.0"
PI_VERSION = "0.87.1"
MAX_DOWNLOAD_BYTES = 150 * 1024 * 1024
NETWORK_TIMEOUT = 60.0
INSTALL_TIMEOUT = 600.0
_CREDENTIAL_ENV = {
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN", "OPENAI_API_KEY", "AZURE_OPENAI_API_KEY",
    "ANT_LING_API_KEY", "DEEPSEEK_API_KEY", "NVIDIA_API_KEY", "GEMINI_API_KEY", "COPILOT_GITHUB_TOKEN",
    "MISTRAL_API_KEY", "GROQ_API_KEY", "CEREBRAS_API_KEY", "XAI_API_KEY", "OPENROUTER_API_KEY",
    "AI_GATEWAY_API_KEY", "ZAI_API_KEY", "ZAI_CODING_CN_API_KEY", "OPENCODE_API_KEY", "RADIUS_API_KEY",
    "HF_TOKEN", "FIREWORKS_API_KEY", "TOGETHER_API_KEY", "BASETEN_API_KEY", "KIMI_API_KEY", "META_API_KEY",
    "MINIMAX_API_KEY", "MINIMAX_CN_API_KEY", "MOONSHOT_API_KEY", "QWEN_TOKEN_PLAN_API_KEY",
    "QWEN_TOKEN_PLAN_CN_API_KEY", "XIAOMI_API_KEY", "XIAOMI_TOKEN_PLAN_CN_API_KEY",
    "XIAOMI_TOKEN_PLAN_AMS_API_KEY", "XIAOMI_TOKEN_PLAN_SGP_API_KEY", "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "GOOGLE_CLOUD_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS", "CLOUDFLARE_API_KEY", "NPM_TOKEN", "NODE_AUTH_TOKEN",
    "PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "PI_PACKAGE_DIR",
}
NODE_BASE = f"https://nodejs.org/dist/v{NODE_VERSION}"

# SHA-256 values published in the pinned official nodejs.org release manifest.
NODE_HASHES = {
    "linux-x64": "fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6",
    "linux-arm64": "6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2",
    "darwin-x64": "0ae5a24c24bb7d015cd816c5036b3f90f2945aa872fcf54e58da054753b3a299",
    "darwin-arm64": "6239d4cf92d864487ec8cd3615038f7b67e7f58b77b21cd2f09ea9fbd68065fe",
    "win-x64": "158f7685b44de51f6c0df1d153526cbcd3e1bc739a8dfc607721cef75de9e541",
}


@dataclass(frozen=True)
class NodeArtifact:
    key: str
    filename: str
    sha256: str
    kind: str
    platform_dir: str


def artifact_for(system: str | None = None, machine: str | None = None) -> NodeArtifact:
    system = system or sys.platform
    system = "linux" if system.startswith("linux") else "win32" if system in ("win32", "windows") else system
    arch = (machine or platform.machine()).lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(arch, arch)
    platform_name = "win" if system == "win32" else system
    key = f"{platform_name}-{arch}"
    if key not in NODE_HASHES:
        raise ValueError(f"Automatic AI setup is unavailable for {system}/{arch}")
    kind = "zip" if system == "win32" else "tar.xz"
    filename = f"node-v{NODE_VERSION}-{key}.{kind}"
    return NodeArtifact(key, filename, NODE_HASHES[key], kind, f"node-v{NODE_VERSION}-{key}")


def data_root(root: str | os.PathLike[str] | None = None) -> Path:
    """Match OpenSCAD's cache base: <cache>/orcad/ai; allow isolated test roots."""
    override = root or os.environ.get("ORCAD_AI_DATA_DIR")
    if override is not None:
        return Path(override).expanduser()
    return openscad_cache_root().parent / "ai"


def _fetch(url: str, opener: Callable[..., Any]):
    response = opener(urllib.request.Request(url, headers={"User-Agent": "orcad-plugin"}), timeout=NETWORK_TIMEOUT)
    if not str(getattr(response, "geturl", lambda: url)()).startswith("https://"):
        response.close()
        raise ValueError("download was redirected to a non-HTTPS URL")
    return response


def _download(artifact: NodeArtifact, destination: Path, opener: Callable[..., Any],
              cancel: threading.Event, progress: Callable[[str, float | None], None]) -> None:
    partial = destination.with_name(destination.name + ".part")
    digest, total = hashlib.sha256(), 0
    try:
        with _fetch(f"{NODE_BASE}/{artifact.filename}", opener) as response, partial.open("wb") as stream:
            size = int(response.headers.get("Content-Length") or 0)
            if size > MAX_DOWNLOAD_BYTES:
                raise ValueError("the Node.js download is unexpectedly large")
            while True:
                if cancel.is_set():
                    raise InterruptedError("AI setup cancelled")
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_DOWNLOAD_BYTES:
                    raise ValueError("the Node.js download is unexpectedly large")
                digest.update(chunk)
                stream.write(chunk)
                progress("Downloading Node.js", round(total / size, 3) if size else None)
        if digest.hexdigest() != artifact.sha256:
            raise ValueError("the Node.js download failed its checksum")
        os.replace(partial, destination)
    finally:
        partial.unlink(missing_ok=True)


def _safe_path(root: Path, name: str) -> Path:
    relative = PurePosixPath(name)
    if "\\" in name or relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("the Node.js archive contains an unsafe path")
    target = root.joinpath(*relative.parts)
    if target.resolve() != root.resolve() and root.resolve() not in target.resolve().parents:
        raise ValueError("the Node.js archive contains an unsafe path")
    return target


def _extract_zip(archive: Path, target: Path) -> None:
    with zipfile.ZipFile(archive) as bundle:
        for info in bundle.infolist():
            path = _safe_path(target, info.filename)
            if stat.S_ISLNK(info.external_attr >> 16):
                raise ValueError("the Node.js archive contains a symlink")
            if info.is_dir():
                path.mkdir(parents=True, exist_ok=True)
            else:
                path.parent.mkdir(parents=True, exist_ok=True)
                with bundle.open(info) as source, path.open("wb") as output:
                    shutil.copyfileobj(source, output)


def _extract_tar(archive: Path, target: Path) -> None:
    target_resolved = target.resolve()
    with tarfile.open(archive, "r:xz") as bundle:
        members = bundle.getmembers()
        for member in members:
            _safe_path(target, member.name)
            if member.issym() or member.islnk():
                link = PurePosixPath(member.linkname)
                if link.is_absolute() or "\\" in member.linkname:
                    raise ValueError("the Node.js archive contains an unsafe link")
                base = PurePosixPath(member.name).parent if member.issym() else PurePosixPath()
                normalized = base.joinpath(link)
                parts: list[str] = []
                for part in normalized.parts:
                    if part == "..":
                        if not parts:
                            raise ValueError("the Node.js archive contains an unsafe link")
                        parts.pop()
                    elif part not in ("", "."):
                        parts.append(part)
                if not parts:
                    raise ValueError("the Node.js archive contains an unsafe link")
                continue
            if not (member.isfile() or member.isdir()):
                raise ValueError("the Node.js archive contains an unsupported entry")
        for member in members:
            path = _safe_path(target, member.name)
            if member.isdir():
                path.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                path.parent.mkdir(parents=True, exist_ok=True)
                source = bundle.extractfile(member)
                if source is None:
                    raise ValueError("the Node.js archive is incomplete")
                with source, path.open("wb") as output:
                    shutil.copyfileobj(source, output)
                path.chmod(member.mode & 0o777)
        for member in members:
            if member.issym():
                path = _safe_path(target, member.name)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.symlink_to(member.linkname)
            elif member.islnk():
                source = _safe_path(target, member.linkname)
                path = _safe_path(target, member.name)
                path.parent.mkdir(parents=True, exist_ok=True)
                os.link(source, path)
    if target.resolve() != target_resolved:
        raise ValueError("the Node.js archive changed its extraction root")


def _run_install(argv: list[str], env: dict[str, str], cancel: threading.Event) -> None:
    prefix = Path(argv[argv.index("--prefix") + 1])
    process = subprocess.Popen(argv, cwd=prefix.parent, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, env=env, **popen_flags())
    try:
        deadline = time.monotonic() + INSTALL_TIMEOUT
        while process.poll() is None:
            if cancel.wait(0.1):
                raise InterruptedError("AI setup cancelled")
            if time.monotonic() > deadline:
                raise TimeoutError("Pi installation timed out")
        if process.returncode:
            raise RuntimeError(f"Pi installation failed with exit code {process.returncode}")
    finally:
        if process.poll() is None:
            process.kill()
            process.wait()


def _node_executable(root: Path, artifact: NodeArtifact) -> Path:
    return root / artifact.platform_dir / ("node.exe" if artifact.key.startswith("win-") else "bin/node")


def _pi_cli(root: Path) -> Path:
    base = root / "pi" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist"
    for candidate in (base / "bundle" / "cli.js", base / "cli.js"):
        if candidate.is_file():
            return candidate
    raise FileNotFoundError("the pinned Pi CLI was not installed")


def _verify_pi(root: Path, node: Path, env: dict[str, str]) -> Path:
    cli = _pi_cli(root)
    package = json.loads((cli.parents[2] / "package.json").read_text(encoding="utf-8"))
    if package.get("version") != PI_VERSION:
        raise RuntimeError("the installed Pi version does not match the pinned release")
    result = subprocess.run([str(node), str(cli), "--version"], stdin=subprocess.DEVNULL,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
                            timeout=30, env=env, **popen_flags())
    if result.returncode:
        raise RuntimeError("the installed Pi CLI could not start")
    return cli


def _installed(root: Path, artifact: NodeArtifact) -> dict[str, str] | None:
    try:
        current = json.loads((root / "current.json").read_text(encoding="utf-8"))
        name = current["runtime"]
        if not isinstance(name, str) or not name.startswith("runtime-") or Path(name).name != name:
            return None
        install = root / name
        marker = json.loads((install / "install.json").read_text(encoding="utf-8"))
        node = _node_executable(install / "runtime", artifact)
        cli = _pi_cli(install)
        if marker.get("node") != NODE_VERSION or marker.get("pi") != PI_VERSION or not node.is_file() or not cli.is_file():
            return None
        package = json.loads((cli.parents[2] / "package.json").read_text(encoding="utf-8"))
        if package.get("version") != PI_VERSION:
            return None
        return {"node": str(node), "pi": str(cli)}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def ensure_ai(*, root: str | os.PathLike[str] | None = None, system: str | None = None,
              machine: str | None = None, opener: Callable[..., Any] = urllib.request.urlopen,
              cancel: threading.Event | None = None,
              progress: Callable[[str, float | None], None] | None = None) -> dict[str, str]:
    """Install exact Node/Pi versions, atomically, entirely below the AI data root."""
    artifact = artifact_for(system, machine)
    destination = data_root(root)
    cancelled = cancel or threading.Event()
    report = progress or (lambda _message, _fraction: None)
    with _INSTALL_LOCK:
        if installed := _installed(destination, artifact):
            return installed
        destination.mkdir(parents=True, exist_ok=True)
        staging = destination / f".install-{os.getpid()}-{threading.get_ident()}"
        shutil.rmtree(staging, ignore_errors=True)
        staging.mkdir()
        archive = staging / artifact.filename
        try:
            _download(artifact, archive, opener, cancelled, report)
            report("Installing Node.js", None)
            runtime = staging / "runtime"
            runtime.mkdir()
            if artifact.kind == "zip":
                _extract_zip(archive, runtime)
            else:
                _extract_tar(archive, runtime)
            archive.unlink()
            node = _node_executable(runtime, artifact)
            if not node.is_file():
                raise FileNotFoundError("node was not found in the official archive")
            pi_dir = staging / "pi"
            pi_dir.mkdir()
            env = {key: value for key, value in child_env().items()
                   if key not in _CREDENTIAL_ENV and not key.startswith("ORCAD_CUSTOM_")}
            env["PATH"] = str(node.parent) + os.pathsep + env.get("PATH", "")
            env["HOME"] = str(staging)
            env["USERPROFILE"] = str(staging)
            env["NPM_CONFIG_CACHE"] = str(staging / "npm-cache")
            env["NPM_CONFIG_USERCONFIG"] = str(staging / "npmrc")
            env["NODE_USE_SYSTEM_CA"] = "1"
            env["NPM_CONFIG_GLOBALCONFIG"] = str(staging / "global-npmrc")
            (staging / "npmrc").touch(mode=0o600)
            (staging / "global-npmrc").touch(mode=0o600)
            npm_cli = runtime / artifact.platform_dir / ("node_modules/npm/bin/npm-cli.js" if artifact.key.startswith("win-")
                                                          else "lib/node_modules/npm/bin/npm-cli.js")
            if not npm_cli.is_file():
                raise FileNotFoundError("npm was not found in the official Node.js archive")
            report("Installing Pi", None)
            command = [str(node), str(npm_cli), "install", "--prefix", str(pi_dir), "--cache",
                       str(staging / "npm-cache"), "--userconfig", str(staging / "npmrc"), "--globalconfig",
                       str(staging / "global-npmrc"), "--registry=https://registry.npmjs.org/", "--no-save", "--no-audit",
                       "--no-fund", "--ignore-scripts", "--loglevel=error", f"@earendil-works/pi-coding-agent@{PI_VERSION}"]
            try:
                _run_install(command, env, cancelled)
            except RuntimeError:
                # npm may fail after writing an optional dependency; trust only an exact, runnable Pi install.
                pass
            cli = _verify_pi(staging, node, env)
            shutil.rmtree(staging / "npm-cache", ignore_errors=True)
            marker = {"node": NODE_VERSION, "pi": PI_VERSION, "artifact": artifact.filename,
                      "sha256": artifact.sha256}
            (staging / "install.json").write_text(json.dumps(marker, sort_keys=True), encoding="utf-8")
            (staging / "install.json").chmod(0o600)
            if cancelled.is_set():
                raise InterruptedError("AI setup cancelled")
            target = destination / f"runtime-{NODE_VERSION}-pi-{PI_VERSION}"
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)
            pointer = destination / "current.json"
            pointer_tmp = pointer.with_name(pointer.name + ".tmp")
            pointer_tmp.write_text(json.dumps({"runtime": target.name}), encoding="utf-8")
            os.replace(pointer_tmp, pointer)
            report("Ready", 1.0)
            return {"node": str(target / "runtime" / artifact.platform_dir / ("node.exe" if artifact.key.startswith("win-") else "bin/node")),
                    "pi": str(target / "pi" / "node_modules" / "@earendil-works" / "pi-coding-agent" / "dist" / "bundle" / "cli.js")}
        except Exception:
            shutil.rmtree(staging, ignore_errors=True)
            raise


_INSTALL_LOCK = threading.Lock()
_STATE_LOCK = threading.Lock()
_DONE = threading.Event()
_CANCEL = threading.Event()
_STATE: dict[str, Any] = {"state": "missing", "node": None, "pi": None, "message": None, "progress": None}


def _worker(root: str | os.PathLike[str] | None, notify: Callable[[], None] | None = None) -> None:
    def progress(message: str, value: float | None) -> None:
        _update(state="installing", message=message, progress=value)
        if notify:
            notify()
    try:
        paths = ensure_ai(root=root, cancel=_CANCEL, progress=progress)
        _update(state="ready", message=None, progress=None, **paths)
        if notify:
            notify()
    except InterruptedError:
        _update(state="missing", message=None, progress=None, node=None, pi=None)
        if notify:
            notify()
    except Exception as exc:
        _update(state="error", message=str(exc)[:400], progress=None, node=None, pi=None)
        if notify:
            notify()
    finally:
        _DONE.set()


def _update(**values: Any) -> None:
    with _STATE_LOCK:
        _STATE.update(values)


def start_ai_bootstrap(root: str | os.PathLike[str] | None = None,
                       notify: Callable[[], None] | None = None) -> None:
    with _STATE_LOCK:
        if _STATE["state"] in ("installing", "ready"):
            return
        _STATE.update(state="installing", message=None, progress=None)
        _DONE.clear()
        _CANCEL.clear()
    threading.Thread(target=_worker, args=(root, notify), name="orcad-ai-setup", daemon=True).start()


def cancel_ai_bootstrap() -> None:
    _CANCEL.set()


def ai_bootstrap_status() -> dict[str, Any]:
    with _STATE_LOCK:
        return dict(_STATE)


def wait_for_ai(*, root: str | os.PathLike[str] | None = None, timeout: float | None = None) -> dict[str, str]:
    start_ai_bootstrap(root)
    if not _DONE.wait(timeout):
        raise TimeoutError("AI setup is still running")
    state = ai_bootstrap_status()
    if state["state"] != "ready":
        raise RuntimeError(state["message"] or "AI setup did not finish")
    return {"node": state["node"], "pi": state["pi"]}
