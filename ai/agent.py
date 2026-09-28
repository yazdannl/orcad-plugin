"""Pi RPC bridge for orcad's single-file OpenSCAD workspace."""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import bootstrap

SYSTEM_PROMPT = """You are orcad's OpenSCAD and 3D-printing CAD assistant. Work only in model.scad in the current workspace. Read the existing model before changing it; preserve useful user work. Put editable parametric variables together at the top, use millimeters, and produce a clean, printable, manifold 3D solid with no self-intersections or non-manifold features. Prefer simple robust CSG. When useful for Gridfinity designs, use the bundled library with include <src/...>; do not invent library paths. After every edit, call render_openscad, inspect compiler errors and warnings and the returned bounding box, correct problems, and render again until successful. Do not claim success without a successful render. Do not use shell commands or access files outside this workspace. Keep the final answer concise."""

PROVIDER_KEY_ENV = {
    "anthropic": "ANTHROPIC_API_KEY", "openai": "OPENAI_API_KEY", "azure-openai": "AZURE_OPENAI_API_KEY",
    "google-vertex": "GOOGLE_CLOUD_API_KEY", "amazon-bedrock": "AWS_BEARER_TOKEN_BEDROCK",
    "cloudflare-workers-ai": "CLOUDFLARE_API_KEY", "cloudflare-ai-gateway": "CLOUDFLARE_API_KEY",
    "ant-ling": "ANT_LING_API_KEY", "deepseek": "DEEPSEEK_API_KEY", "nvidia": "NVIDIA_API_KEY",
    "google": "GEMINI_API_KEY", "google-gemini": "GEMINI_API_KEY", "github-copilot": "COPILOT_GITHUB_TOKEN",
    "mistral": "MISTRAL_API_KEY", "groq": "GROQ_API_KEY", "cerebras": "CEREBRAS_API_KEY",
    "xai": "XAI_API_KEY", "openrouter": "OPENROUTER_API_KEY", "vercel-ai-gateway": "AI_GATEWAY_API_KEY",
    "zai": "ZAI_API_KEY", "zai-coding-plan": "ZAI_API_KEY", "zai-coding-plan-cn": "ZAI_CODING_CN_API_KEY",
    "opencode": "OPENCODE_API_KEY", "opencode-zen": "OPENCODE_API_KEY", "radius": "RADIUS_API_KEY",
    "huggingface": "HF_TOKEN", "fireworks": "FIREWORKS_API_KEY", "together": "TOGETHER_API_KEY",
    "baseten": "BASETEN_API_KEY", "kimi": "KIMI_API_KEY", "kimi-for-coding": "KIMI_API_KEY",
    "meta": "META_API_KEY", "minimax": "MINIMAX_API_KEY", "minimax-cn": "MINIMAX_CN_API_KEY",
    "moonshot": "MOONSHOT_API_KEY", "qwen": "QWEN_TOKEN_PLAN_API_KEY", "qwen-cn": "QWEN_TOKEN_PLAN_CN_API_KEY",
    "xiaomi": "XIAOMI_API_KEY", "xiaomi-cn": "XIAOMI_TOKEN_PLAN_CN_API_KEY",
    "xiaomi-ams": "XIAOMI_TOKEN_PLAN_AMS_API_KEY", "xiaomi-sgp": "XIAOMI_TOKEN_PLAN_SGP_API_KEY",
    "qwen-token-plan": "QWEN_TOKEN_PLAN_API_KEY", "qwen-token-plan-cn": "QWEN_TOKEN_PLAN_CN_API_KEY",
    "xiaomi-mimo": "XIAOMI_API_KEY", "xiaomi-mimo-cn": "XIAOMI_TOKEN_PLAN_CN_API_KEY",
}

# These provider credentials are deliberately removed from inherited host env.
_SECRET_ENV = set(PROVIDER_KEY_ENV.values()) | {
    "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_OAUTH_TOKEN", "AWS_ACCESS_KEY_ID", "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN", "AWS_BEARER_TOKEN_BEDROCK", "GOOGLE_CLOUD_API_KEY", "GOOGLE_APPLICATION_CREDENTIALS",
    "CLOUDFLARE_API_KEY", "CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_GATEWAY_ID", "AZURE_OPENAI_BASE_URL",
    "AZURE_OPENAI_RESOURCE_NAME", "AZURE_OPENAI_API_VERSION", "AZURE_OPENAI_DEPLOYMENT_NAME_MAP",
    "NPM_TOKEN", "NODE_AUTH_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
}
_PI_ENV = {"PI_CODING_AGENT_DIR", "PI_CODING_AGENT_SESSION_DIR", "PI_PACKAGE_DIR", "PI_OFFLINE",
           "PI_TELEMETRY", "PI_SKIP_VERSION_CHECK"}


def _atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + f".{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temp, mode)
        os.replace(temp, path)
        os.chmod(path, mode)
    finally:
        temp.unlink(missing_ok=True)


def _clean_pi_environment(base: dict[str, str], config: dict[str, Any], root: Path) -> dict[str, str]:
    env = {key: value for key, value in base.items() if key not in _SECRET_ENV and key not in _PI_ENV}
    env["PATH"] = os.pathsep.join((str(Path(config["node"]).parent), env.get("PATH", ""))).rstrip(os.pathsep)
    env["PI_CODING_AGENT_SESSION_DIR"] = str(root / "sessions")
    env["PI_TELEMETRY"] = "0"
    env["PI_SKIP_VERSION_CHECK"] = "1"
    env["NODE_USE_SYSTEM_CA"] = "1"
    env["OPENSCAD_BIN"] = config["openscad"]
    env["OPENSCADPATH"] = config["library"]
    if config.get("backend_flag"):
        env["OPENSCAD_BACKEND_FLAG"] = config["backend_flag"]
    env["PI_CODING_AGENT_DIR"] = (str(root / "private-agent") if config["source"] == "key"
                                  else str(Path.home() / ".pi" / "agent"))
    if config["source"] == "key" and config.get("provider") == config.get("key_provider") and config.get("provider"):
        key_env = PROVIDER_KEY_ENV.get(config["provider"])
        if key_env:
            key_path = root / "provider-key"
            if key_path.is_file():
                env[key_env] = key_path.read_text(encoding="utf-8")
    return env


class PiAgent:
    """Single long-lived RPC process; all disk/process work is done by workers."""

    def __init__(self, post: Callable[[dict[str, Any]], None], *, root: str | os.PathLike[str] | None = None,
                 library: str | os.PathLike[str], openscad: Callable[[], str | os.PathLike[str] | None],
                 child_env: Callable[[], dict[str, str]] | None = None,
                 command_factory: Callable[[dict[str, Any]], list[str]] | None = None):
        self.post = post
        self.root = bootstrap.data_root(root)
        self.workspace = self.root / "workspace"
        self.library = Path(library)
        self.openscad = openscad
        self.base_env = child_env or _default_child_env
        self.command_factory = command_factory
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._spawn_lock = threading.Lock()
        self._config: dict[str, Any] = {"source": "pi", "provider": None, "model": None, "thinking": None,
                                        "key_provider": None, "has_key": False, "node": None, "pi": None}
        self._models: list[dict[str, str]] = []
        self._process: subprocess.Popen[bytes] | None = None
        self._pending: dict[str, tuple[threading.Event, dict[str, Any]]] = {}
        self._request_no = 0
        self._active_id: str | None = None
        self._configuring = 0
        self._active_error: str | None = None
        self._cancelled = False
        self._closed = False
        self._loaded = False
        self._configure_lock = threading.Lock()
        self._load_lock = threading.Lock()
        self._initializing = False
        self._reader: threading.Thread | None = None

    def status(self) -> dict[str, Any]:
        setup = bootstrap.ai_bootstrap_status()
        with self._lock:
            config = {key: self._config.get(key) for key in ("source", "provider", "model", "thinking", "has_key")}
            models = list(self._models)
            busy = self._active_id is not None or self._configuring > 0
        message = setup.get("message")
        return {"type": "ai_status", "state": setup["state"], "message": message,
                "progress": setup.get("progress"), "node_version": bootstrap.NODE_VERSION if setup["state"] == "ready" else None,
                "pi_version": bootstrap.PI_VERSION if setup["state"] == "ready" else None,
                "busy": busy, "config": config, "models": models}

    def setup(self) -> None:
        bootstrap.start_ai_bootstrap(self.root, notify=self._on_setup_update)
        threading.Thread(target=self._load_config, name="orcad-ai-config", daemon=True).start()

    def _on_setup_update(self) -> None:
        self.post(self.status())
        if bootstrap.ai_bootstrap_status()["state"] == "ready":
            with self._lock:
                if self._initializing or self._closed:
                    return
                self._initializing = True
            threading.Thread(target=self._initialize_worker, name="orcad-ai-models", daemon=True).start()

    def _initialize_worker(self) -> None:
        try:
            self._load_config()
            self._ensure_process()
            self._refresh_models()
        except Exception:
            pass
        finally:
            with self._lock:
                self._initializing = False
            self.post(self.status())

    def _load_config(self) -> None:
        with self._load_lock:
            if self._loaded:
                return
            try:
                path = self.root / "config.json"
                values = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else {}
                if values.get("source") in ("pi", "key"):
                    with self._lock:
                        self._config.update({key: values.get(key) for key in
                                             ("source", "provider", "model", "thinking", "key_provider")})
                        self._config["has_key"] = (self.root / "provider-key").is_file()
            except (OSError, ValueError, TypeError):
                pass
            self._loaded = True
        self.post(self.status())

    def configure(self, msg: dict[str, Any]) -> None:
        source = msg.get("source", "pi")
        provider = msg.get("provider")
        model = msg.get("model")
        thinking = msg.get("thinking")
        key = msg.get("api_key")
        if source not in ("pi", "key") or (provider is not None and not isinstance(provider, str)) \
                or (model is not None and not isinstance(model, str)) or (thinking is not None and not isinstance(thinking, str)) \
                or (key is not None and not isinstance(key, str)):
            self.post({"type": "ai_status", **self.status(), "message": "Invalid AI configuration"})
            return
        if source == "key" and provider and provider not in PROVIDER_KEY_ENV:
            self.post({**self.status(), "message": "This provider does not support API-key configuration"})
            return
        with self._lock:
            self._configuring += 1
        threading.Thread(target=self._configure_worker, args=(source, provider, model, thinking, key),
                         name="orcad-ai-configure", daemon=True).start()

    def _configure_worker(self, source: str, provider: str | None, model: str | None,
                          thinking: str | None, key: str | None) -> None:
        try:
            with self._configure_lock:
                self._load_config()
                with self._lock:
                    active_id = self._active_id
                if active_id is not None:
                    self.abort(active_id)
                    self._wait_idle(30)
                if source == "key" and key:
                    if provider not in PROVIDER_KEY_ENV:
                        self.post({**self.status(), "message": "This provider does not support API-key configuration"})
                        return
                    _atomic_write(self.root / "provider-key", key.encode("utf-8"), 0o600)
                    key_provider = provider
                else:
                    key_provider = self._config.get("key_provider")
                updated = {"source": source, "provider": provider, "model": model, "thinking": thinking,
                           "key_provider": key_provider}
                _atomic_write(self.root / "config.json", json.dumps(updated, sort_keys=True).encode("utf-8"), 0o600)
                with self._lock:
                    self._config.update(updated)
                    self._config["has_key"] = (self.root / "provider-key").is_file()
                self._stop_process()
                if bootstrap.ai_bootstrap_status()["state"] == "ready":
                    try:
                        self._ensure_process()
                        self._refresh_models()
                    except Exception:
                        pass
        except Exception:
            self.post({**self.status(), "message": "Could not update AI settings"})
        finally:
            with self._lock:
                self._configuring = max(0, self._configuring - 1)
            self.post(self.status())

    @property
    def busy(self) -> bool:
        with self._lock:
            return self._active_id is not None

    def prompt(self, run_id: Any, text: Any, code: Any) -> bool:
        if not isinstance(run_id, str) or not isinstance(text, str) or not isinstance(code, str):
            self.post({"type": "ai_done", "id": run_id, "ok": False, "error": "invalid request", "code": ""})
            return True
        with self._lock:
            if self._active_id is not None or self._configuring > 0:
                return False
            self._active_id = run_id
            self._active_error = None
            self._cancelled = False
        threading.Thread(target=self._prompt_worker, args=(run_id, text, code),
                         name="orcad-ai-prompt", daemon=True).start()
        return True

    def _prompt_worker(self, run_id: str, text: str, code: str) -> None:
        try:
            self._load_config()
            bootstrap.start_ai_bootstrap(self.root, notify=lambda: self.post(self.status()))
            paths = bootstrap.wait_for_ai(root=self.root)
            openscad = self.openscad()
            if self._is_cancelled(run_id):
                self._finish(run_id, False, "cancelled")
                return
            if not openscad:
                raise RuntimeError("OpenSCAD is not ready")
            self._config.update(node=paths["node"], pi=paths["pi"])
            self.workspace.mkdir(parents=True, exist_ok=True)
            _atomic_write(self.workspace / "model.scad", code.encode("utf-8"))
            self._ensure_process()
            self._refresh_models()
            if self._is_cancelled(run_id):
                self._finish(run_id, False, "cancelled")
                return
            with self._lock:
                self._active_error = None
            prompt = ("Update model.scad to satisfy this request. The current editor source is already in that file.\n\n"
                      + text + "\n\nRead model.scad, make the smallest suitable OpenSCAD change, then render and iterate.")
            response = self._rpc("prompt", message=prompt)
            if not response.get("success"):
                self._finish(run_id, False, "Pi rejected the prompt")
        except Exception:
            self._finish(run_id, False, "AI setup or RPC failed")

    def abort(self, run_id: Any) -> None:
        with self._lock:
            if self._active_id is None or run_id != self._active_id:
                return
            self._cancelled = True
            starting = self._process is None
        if starting:
            bootstrap.cancel_ai_bootstrap()
        threading.Thread(target=self._abort_worker, name="orcad-ai-abort", daemon=True).start()

    def _abort_worker(self) -> None:
        try:
            if self._process and self._process.poll() is None:
                self._rpc("abort", timeout=30)
        except Exception:
            self._finish(self._active_id, False, "cancelled")

    def reset(self) -> None:
        threading.Thread(target=self._reset_worker, name="orcad-ai-reset", daemon=True).start()

    def _reset_worker(self) -> None:
        active = self._active_id
        if active is not None:
            self.abort(active)
            self._wait_idle(30)
        try:
            self._ensure_process()
            self._rpc("new_session", timeout=30)
        except Exception:
            pass
        self.post(self.status())

    def _is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return self._active_id != run_id or self._cancelled

    def _wait_idle(self, timeout: float) -> None:
        deadline = time.monotonic() + timeout
        while self.busy and time.monotonic() < deadline:
            time.sleep(0.05)

    def _ensure_process(self, extension: Path | None = None) -> None:
        with self._spawn_lock:
            self._ensure_process_locked(extension)

    def _ensure_process_locked(self, extension: Path | None = None) -> None:
        with self._lock:
            if self._closed:
                raise RuntimeError("AI agent is shut down")
            if self._process is not None and self._process.poll() is None:
                return
        if not self._loaded:
            self._load_config()
        paths = bootstrap.wait_for_ai(root=self.root)
        openscad = self.openscad()
        if not openscad:
            raise RuntimeError("OpenSCAD is not ready")
        self._config.update(node=paths["node"], pi=paths["pi"])
        extension = extension or Path(paths["pi"]).parents[2] / "orcad-render.ts"
        bundled = Path(__file__).with_name("render_openscad.ts")
        if not bundled.is_file():
            raise RuntimeError("The OpenSCAD render extension is missing")
        if not extension.is_file() or bundled.stat().st_mtime_ns > extension.stat().st_mtime_ns:
            _atomic_write(extension, bundled.read_bytes(), 0o600)
        try:
            from orcad_openscad import backend_args, probe_openscad
        except ImportError:
            from openscad import backend_args, probe_openscad
        version = probe_openscad(openscad).version
        flags = backend_args(version)
        env = _clean_pi_environment({**self.base_env(), "OPENSCADPATH": str(self.library)},
                                    {**self._config, "openscad": str(openscad), "library": str(self.library),
                                     "backend_flag": flags[0] if flags else ""}, self.root)
        if self.command_factory:
            command = self.command_factory(self._config)
        else:
            command = [paths["node"], paths["pi"], "--mode", "rpc", "--no-session", "--tools",
                       "read,edit,write,render_openscad", "--extension", str(extension), "--no-extensions",
                       "--no-context-files", "--no-skills", "--no-prompt-templates", "--no-themes", "--no-approve",
                       "--system-prompt", SYSTEM_PROMPT]
            if self._config.get("provider"):
                command += ["--provider", self._config["provider"]]
            if self._config.get("model"):
                command += ["--model", self._config["model"]]
            if self._config.get("thinking"):
                command += ["--thinking", self._config["thinking"]]
        self.workspace.mkdir(parents=True, exist_ok=True)
        process = subprocess.Popen(command, cwd=self.workspace, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env, **bootstrap.popen_flags())
        with self._lock:
            if self._closed:
                process.kill()
                raise RuntimeError("AI agent is shut down")
            self._process = process
            self._reader = threading.Thread(target=self._read_loop, args=(process,), name="orcad-pi-rpc", daemon=True)
            self._reader.start()

    def _refresh_models(self) -> None:
        response = self._rpc("get_available_models", timeout=30)
        models = response.get("data", {}).get("models", []) if response.get("success") else []
        with self._lock:
            self._models = [{"provider": str(item.get("provider", "")), "id": str(item.get("id", "")),
                             "name": str(item.get("name", item.get("id", "")))}
                            for item in models if item.get("provider") and item.get("id")]

    def _rpc(self, command: str, *, timeout: float = 20, **values: Any) -> dict[str, Any]:
        with self._lock:
            self._request_no += 1
            request_id = f"orcad-{self._request_no}"
            event, result = threading.Event(), {}
            self._pending[request_id] = (event, result)
        self._send({"id": request_id, "type": command, **values})
        if not event.wait(timeout):
            with self._lock:
                self._pending.pop(request_id, None)
            raise TimeoutError("Pi RPC timed out")
        if not result.get("success"):
            raise RuntimeError("Pi RPC command failed")
        return result

    def _send(self, message: dict[str, Any]) -> None:
        with self._write_lock:
            process = self._process
            if process is None or process.poll() is not None or process.stdin is None:
                raise RuntimeError("Pi RPC process is unavailable")
            process.stdin.write(json.dumps(message, ensure_ascii=True).encode("utf-8") + b"\n")
            process.stdin.flush()

    def _read_loop(self, process: subprocess.Popen[bytes]) -> None:
        try:
            assert process.stdout is not None
            for raw in process.stdout:
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                if record.get("type") == "response":
                    request_id = record.get("id")
                    with self._lock:
                        pending = self._pending.pop(request_id, None)
                    if pending:
                        pending[1].update(record)
                        pending[0].set()
                else:
                    self._event(record)
        finally:
            with self._lock:
                active = self._active_id
            if active is not None:
                self._finish(active, False, "Pi agent stopped")

    def _event(self, event: dict[str, Any]) -> None:
        run_id = self._active_id
        if run_id is None:
            return
        kind = event.get("type")
        if kind == "message_update":
            update = event.get("assistantMessageEvent") or {}
            if update.get("type") in ("text_delta", "thinking_delta") and update.get("delta"):
                self.post({"type": "ai_event", "id": run_id,
                           "kind": "text" if update["type"] == "text_delta" else "thinking",
                           "delta": update["delta"]})
        elif kind == "tool_execution_start":
            self.post({"type": "ai_event", "id": run_id, "kind": "tool",
                       "call_id": event.get("toolCallId", ""), "name": event.get("toolName", ""),
                       "phase": "start", "summary": _tool_summary(event.get("toolName"), event.get("args"))})
        elif kind == "tool_execution_end":
            name = event.get("toolName", "")
            self.post({"type": "ai_event", "id": run_id, "kind": "tool",
                       "call_id": event.get("toolCallId", ""), "name": name, "phase": "end",
                       "summary": _tool_summary(name, event.get("result")), "is_error": bool(event.get("isError"))})
            if name in ("edit", "write"):
                try:
                    code = (self.workspace / "model.scad").read_text(encoding="utf-8")
                    self.post({"type": "ai_code", "id": run_id, "code": code})
                except OSError:
                    pass
        elif kind == "message_end":
            message = event.get("message", {})
            if message.get("role") == "assistant" and message.get("stopReason") in ("error", "aborted"):
                self._active_error = "Pi agent stopped before completing"
        elif kind == "auto_retry_end" and event.get("success") is False:
            self._active_error = "Provider request failed"
        elif kind == "agent_settled":
            error = self._active_error
            cancelled = self._cancelled
            self._finish(run_id, not error and not cancelled, "cancelled" if cancelled else error)

    def _finish(self, run_id: str | None, ok: bool, error: str | None = None) -> None:
        if run_id is None:
            return
        with self._lock:
            if self._active_id != run_id:
                return
            self._active_id = None
            cancelled, self._cancelled = self._cancelled, False
            self._active_error = None
        try:
            code = (self.workspace / "model.scad").read_text(encoding="utf-8")
        except OSError:
            code = ""
        self.post({"type": "ai_done", "id": run_id, "ok": bool(ok and not cancelled),
                   **({"error": "cancelled" if cancelled else error} if cancelled or error else {}), "code": code})
        self.post(self.status())

    def _stop_process(self) -> None:
        with self._spawn_lock:
            self._stop_process_locked()

    def _stop_process_locked(self) -> None:
        with self._lock:
            process, self._process = self._process, None
            self._models = []
        if process is None:
            return
        try:
            if process.stdin:
                process.stdin.close()
            process.wait(timeout=3)
        except Exception:
            process.kill()
            process.wait()

    def close(self) -> None:
        with self._lock:
            self._closed = True
            active = self._active_id
            self._cancelled = active is not None
        if active is not None:
            self.abort(active)
        if bootstrap.ai_bootstrap_status()["state"] == "installing":
            bootstrap.cancel_ai_bootstrap()
        self._stop_process()


def _tool_summary(name: Any, details: Any) -> str:
    if name == "render_openscad":
        return "Render OpenSCAD"
    if name in ("edit", "write"):
        return f"{name.title()} model.scad"
    return str(name or "Tool")


def _default_child_env() -> dict[str, str]:
    try:
        from orcad_openscad.runner import child_env
    except ImportError:
        from openscad.runner import child_env
    return child_env()
