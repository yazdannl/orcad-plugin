"""Pi RPC bridge for orcad's single-file OpenSCAD workspace."""
from __future__ import annotations

import ipaddress
import json
import os
import re
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlsplit

from . import bootstrap, providers as custom_provider_config

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
    env = {key: value for key, value in base.items()
           if key not in _SECRET_ENV and key not in _PI_ENV and not key.startswith("ORCAD_CUSTOM_")}
    env["PATH"] = os.pathsep.join((str(Path(config["node"]).parent), env.get("PATH", ""))).rstrip(os.pathsep)
    env["PI_CODING_AGENT_SESSION_DIR"] = str(root / "sessions")
    env["PI_TELEMETRY"] = "0"
    env["PI_SKIP_VERSION_CHECK"] = "1"
    env["NODE_USE_SYSTEM_CA"] = "1"
    env["OPENSCAD_BIN"] = config["openscad"]
    env["OPENSCADPATH"] = config["library"]
    if config.get("backend_flag"):
        env["OPENSCAD_BACKEND_FLAG"] = config["backend_flag"]
    env["PI_CODING_AGENT_DIR"] = (str(Path.home() / ".pi" / "agent") if config["source"] == "pi"
                                  else str(root / "private-agent"))
    if config["source"] == "key" and config.get("provider") == config.get("key_provider") and config.get("provider"):
        key_env = PROVIDER_KEY_ENV.get(config["provider"])
        if key_env:
            key_path = root / "provider-key"
            if key_path.is_file():
                env[key_env] = key_path.read_text(encoding="utf-8")
    if config["source"] != "pi":
        metadata = Path(env["PI_CODING_AGENT_DIR"]) / "custom-providers.json"
        try:
            providers = json.loads(metadata.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            providers = []
        if isinstance(providers, list):
            for item in providers:
                if not isinstance(item, dict) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", str(item.get("id", ""))):
                    continue
                provider_id = item["id"]
                env_name = "ORCAD_CUSTOM_" + provider_id.upper().replace("-", "_") + "_API_KEY"
                key_path = Path(env["PI_CODING_AGENT_DIR"]) / "custom-keys" / f"{provider_id}.key"
                env[env_name] = key_path.read_text(encoding="utf-8") if key_path.is_file() else "orcad-local"
    return env


class PiAgent:
    """Single long-lived RPC process; all disk/process work is done by workers."""

    def __init__(self, post: Callable[[dict[str, Any]], None], *, root: str | os.PathLike[str] | None = None,
                 library: str | os.PathLike[str], openscad: Callable[[], str | os.PathLike[str] | None],
                 child_env: Callable[[], dict[str, str]] | None = None,
                 command_factory: Callable[[dict[str, Any]], list[str]] | None = None,
                 auth_helper_factory: Callable[[str, str, str, str, str, str], list[str]] | None = None):
        self.post = post
        self.root = bootstrap.data_root(root)
        self.workspace = self.root / "workspace"
        self.library = Path(library)
        self.openscad = openscad
        self.base_env = child_env or _default_child_env
        self.command_factory = command_factory
        self.auth_helper_factory = auth_helper_factory
        self._lock = threading.RLock()
        self._write_lock = threading.Lock()
        self._spawn_lock = threading.Lock()
        self._config: dict[str, Any] = {"source": "pi", "provider": None, "model": None, "thinking": None,
                                        "key_provider": None, "has_key": False, "node": None, "pi": None}
        self._models: list[dict[str, str]] = []
        self._providers: list[dict[str, Any]] = []
        self._custom_providers: list[dict[str, Any]] = []
        self._auth_process: subprocess.Popen[bytes] | None = None
        self._auth_dialog: tuple[str, threading.Event, dict[str, Any], dict[str, Any]] | None = None
        self._auth_request_no = 0
        self._auth_busy = False
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
            if config["source"] == "key":
                config["source"] = "managed"
            models = list(self._models)
            providers = [dict(provider) for provider in self._providers]
            custom = [dict(provider) for provider in self._custom_providers]
            busy = self._active_id is not None or self._configuring > 0 or self._auth_busy
        message = setup.get("message")
        return {"type": "ai_status", "state": setup["state"], "message": message,
                "progress": setup.get("progress"), "node_version": bootstrap.NODE_VERSION if setup["state"] == "ready" else None,
                "pi_version": bootstrap.PI_VERSION if setup["state"] == "ready" else None,
                "busy": busy, "auth_busy": self._auth_busy, "config": config, "models": models,
                "providers": providers, "custom_providers": custom}

    def _load_custom_providers(self) -> None:
        path = self.root / "private-agent" / "custom-providers.json"
        try:
            records = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            records = []
        key_root = path.parent / "custom-keys"
        result = []
        if isinstance(records, list):
            for item in records:
                if not isinstance(item, dict) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", str(item.get("id", ""))):
                    continue
                models = item.get("models", [])
                safe_models = [{"id": model["id"], "name": model.get("name", model["id"])}
                               for model in models if isinstance(model, dict) and isinstance(model.get("id"), str)] \
                    if isinstance(models, list) else []
                result.append({"id": item["id"], "name": str(item.get("name", item["id"]))[:80],
                               "baseUrl": str(item.get("baseUrl", ""))[:500], "api": str(item.get("api", "")),
                               "models": safe_models,
                               "has_key": (key_root / f"{item['id']}.key").is_file()})
        with self._lock:
            self._custom_providers = result

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
            if self._config.get("source") != "pi":
                self._load_custom_providers()
            self._ensure_process()
            self._refresh_models()
            self._refresh_provider_catalog()
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
                if values.get("source") in ("pi", "key", "managed"):
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
        if source not in ("pi", "key", "managed") or (provider is not None and not isinstance(provider, str)) \
                or (model is not None and not isinstance(model, str)) or (thinking is not None and not isinstance(thinking, str)) \
                or (key is not None and not isinstance(key, str)):
            self.post({"type": "ai_status", **self.status(), "message": "Invalid AI configuration"})
            return
        if source == "key" and provider and provider not in PROVIDER_KEY_ENV:
            self.post({**self.status(), "message": "This provider does not support API-key configuration"})
            return
        with self._lock:
            if self._auth_busy:
                self.post({**self.status(), "message": "Finish the current sign-in first"})
                return
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
                if source != "pi":
                    self._load_custom_providers()
                if bootstrap.ai_bootstrap_status()["state"] == "ready":
                    try:
                        self._ensure_process()
                        self._refresh_models()
                        self._refresh_provider_catalog()
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
            if self._active_id is not None or self._configuring > 0 or self._auth_busy:
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

    def _custom_records(self) -> list[dict[str, Any]]:
        path = self.root / "private-agent" / "custom-providers.json"
        try:
            items = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        if not isinstance(items, list):
            return []
        return [item for item in items if isinstance(item, dict)
                and re.fullmatch(r"[a-z][a-z0-9-]{0,39}", str(item.get("id", "")))]

    def detect_custom_models(self, message: dict[str, Any]) -> bool:
        base_url, api_key, request_id = message.get("baseUrl"), message.get("api_key"), message.get("id")
        if not isinstance(base_url, str) or (api_key is not None and not isinstance(api_key, str)) \
                or not isinstance(request_id, str) or len(request_id) > 100:
            return False
        with self._lock:
            if self._config.get("source") == "pi" or self._active_id is not None or self._configuring or self._auth_busy:
                return False
            self._configuring += 1
        threading.Thread(target=self._detect_custom_models_worker, args=(request_id, base_url, api_key),
                         name="orcad-provider-detect", daemon=True).start()
        return True

    def _detect_custom_models_worker(self, request_id: Any, base_url: str, api_key: str | None) -> None:
        try:
            models = custom_provider_config.detect_models(base_url, api_key)
            result = {"type": "ai_provider_detect_result", "id": request_id, "ok": True, "models": models}
        except ValueError as exc:
            result = {"type": "ai_provider_detect_result", "id": request_id, "ok": False, "error": str(exc)}
        except Exception:
            result = {"type": "ai_provider_detect_result", "id": request_id, "ok": False,
                      "error": "Model discovery failed."}
        finally:
            with self._lock:
                self._configuring = max(0, self._configuring - 1)
        self.post(result)
        self.post(self.status())

    def save_custom_provider(self, message: dict[str, Any]) -> bool:
        record = message.get("provider")
        key = message.get("api_key")
        if not isinstance(record, dict) or (key is not None and not isinstance(key, str)):
            return False
        with self._lock:
            if self._config.get("source") == "pi" or self._active_id is not None or self._configuring or self._auth_busy:
                return False
            self._configuring += 1
        threading.Thread(target=self._custom_provider_worker,
                         args=("save", record, key, message.get("remove_key") is True),
                         name="orcad-provider-save", daemon=True).start()
        return True

    def remove_custom_provider(self, message: dict[str, Any]) -> bool:
        provider_id = message.get("id")
        if not isinstance(provider_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", provider_id):
            return False
        with self._lock:
            if self._config.get("source") == "pi" or self._active_id is not None or self._configuring or self._auth_busy:
                return False
            self._configuring += 1
        threading.Thread(target=self._custom_provider_worker, args=("remove", {"id": provider_id}, None, False),
                         name="orcad-provider-remove", daemon=True).start()
        return True

    def _custom_provider_worker(self, action: str, record: dict[str, Any], key: str | None, remove_key: bool) -> None:
        provider_id = str(record.get("id", ""))
        ok, error = False, None
        try:
            with self._configure_lock:
                self._load_config()
                if self._config.get("source") == "pi":
                    raise ValueError("Choose orcad-managed mode first")
                directory = self.root / "private-agent"
                directory.mkdir(parents=True, exist_ok=True, mode=0o700)
                os.chmod(directory, 0o700)
                records = self._custom_records()
                if action == "remove":
                    if not any(item["id"] == provider_id for item in records):
                        raise ValueError("Provider not found")
                    records = [item for item in records if item["id"] != provider_id]
                else:
                    name = record.get("name")
                    if not isinstance(name, str) or not name.strip() or len(name.strip()) > 80:
                        raise ValueError("Enter a provider name (up to 80 characters)")
                    provider_id = record.get("id") or custom_provider_config.custom_provider_id(name)
                    if not isinstance(provider_id, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", provider_id):
                        raise ValueError("Provider ID is invalid")
                    existing = next((item for item in records if item["id"] == provider_id), None)
                    if record.get("id") and existing is None:
                        raise ValueError("Provider not found")
                    if not record.get("id"):
                        stem, suffix = provider_id, 2
                        while any(item["id"] == provider_id for item in records):
                            provider_id = f"{stem[:36]}-{suffix}"
                            suffix += 1
                    base_url = custom_provider_config.validate_base_url(record.get("baseUrl", ""))
                    api = record.get("api")
                    if api not in custom_provider_config.API_TYPES:
                        raise ValueError("Choose a supported API type")
                    models = record.get("models")
                    if isinstance(models, str):
                        models = [{"id": item.strip(), "name": item.strip()} for item in models.splitlines() if item.strip()]
                    models = custom_provider_config._clean_models(models)
                    safe = {"id": provider_id, "name": name.strip(), "baseUrl": base_url, "api": api, "models": models}
                    if existing:
                        records = [safe if item["id"] == provider_id else item for item in records]
                    else:
                        records.append(safe)
                    key_path = directory / "custom-keys" / f"{provider_id}.key"
                    if key:
                        if len(key) > 4096 or "\r" in key or "\n" in key:
                            raise ValueError("The API key is too long or contains a line break")
                        key_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
                        os.chmod(key_path.parent, 0o700)
                        _atomic_write(key_path, key.encode("utf-8"), 0o600)
                    elif remove_key:
                        key_path.unlink(missing_ok=True)
                models_config = custom_provider_config.build_models_config(records)
                _atomic_write(directory / "models.json", json.dumps(models_config, sort_keys=True).encode("utf-8"), 0o600)
                _atomic_write(directory / "custom-providers.json", json.dumps(records, sort_keys=True).encode("utf-8"), 0o600)
                if action == "remove":
                    (directory / "custom-keys" / f"{provider_id}.key").unlink(missing_ok=True)
                self._stop_process()
                self._load_custom_providers()
                if bootstrap.ai_bootstrap_status()["state"] == "ready":
                    try:
                        self._ensure_process()
                        self._refresh_models()
                        self._refresh_provider_catalog()
                    except Exception:
                        pass
                ok = True
        except ValueError as exc:
            error = str(exc)
        except Exception:
            error = "Could not save the custom provider settings"
        finally:
            with self._lock:
                self._configuring = max(0, self._configuring - 1)
        self.post({"type": "ai_provider_result", "action": action, "id": provider_id,
                   "ok": ok, **({"error": error} if error else {})})
        self.post(self.status())

    def _private_agent_dir(self) -> Path:
        return Path.home() / ".pi" / "agent" if self._config.get("source") == "pi" else self.root / "private-agent"

    def _auth_helper_command(self, paths: dict[str, str], agent_dir: Path, action: str,
                             provider_id: str, auth_type: str) -> list[str]:
        if self.auth_helper_factory:
            return self.auth_helper_factory(paths["node"], paths["pi"], str(agent_dir), action, provider_id, auth_type)
        package_root = str(Path(paths["pi"]).parents[2])
        helper = Path(__file__).with_name("pi_auth_helper.mjs")
        return [paths["node"], str(helper), package_root, str(agent_dir), action, provider_id, auth_type]

    @staticmethod
    def _safe_auth_url(value: Any) -> str:
        if not isinstance(value, str) or len(value) > 2048 or any(ord(char) < 0x20 for char in value):
            return ""
        try:
            parsed = urlsplit(value)
        except ValueError:
            return ""
        if not parsed.hostname or parsed.username is not None or parsed.password is not None:
            return ""
        if parsed.scheme == "https":
            return value
        if parsed.scheme == "http":
            host = parsed.hostname.lower()
            if host in ("localhost", "host.docker.internal") or host.endswith(".localhost"):
                return value
            try:
                if ipaddress.ip_address(host).is_loopback:
                    return value
            except ValueError:
                pass
        return ""

    @staticmethod
    def _auth_notice(event: Any) -> dict[str, Any] | None:
        if not isinstance(event, dict):
            return None
        kind = event.get("type")
        if kind == "device_code":
            uri = PiAgent._safe_auth_url(event.get("verificationUri"))
            if not uri:
                return None
            return {"type": kind, "userCode": str(event.get("userCode", ""))[:100],
                    "verificationUri": uri,
                    "intervalSeconds": event.get("intervalSeconds"), "expiresInSeconds": event.get("expiresInSeconds")}
        if kind == "auth_url":
            url = PiAgent._safe_auth_url(event.get("url"))
            return ({"type": kind, "url": url, "instructions": str(event.get("instructions", ""))[:2000]}
                    if url else None)
        if kind in ("info", "progress"):
            links = event.get("links") if kind == "info" else None
            return {"type": kind, "message": str(event.get("message", ""))[:2000],
                    "links": [{"url": url, "label": str(link.get("label", ""))[:200]}
                              for link in links[:10] if isinstance(link, dict)
                              for url in [PiAgent._safe_auth_url(link.get("url"))] if url] if isinstance(links, list) else []}
        return None

    def _run_auth_helper(self, action: str, provider_id: str = "", auth_type: str = "") -> dict[str, Any]:
        paths = bootstrap.wait_for_ai(root=self.root)
        agent_dir = self._private_agent_dir()
        agent_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(agent_dir, 0o700)
        env = _clean_pi_environment({**self.base_env(), "OPENSCADPATH": str(self.library)},
                                    {**self._config, "node": paths["node"], "openscad": "",
                                     "library": str(self.library)}, self.root)
        command = self._auth_helper_command(paths, agent_dir, action, provider_id, auth_type)
        process = subprocess.Popen(command, cwd=agent_dir, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                   stderr=subprocess.DEVNULL, env=env, **bootstrap.popen_flags())
        with self._lock:
            self._auth_process = process
        result: dict[str, Any] = {}
        try:
            assert process.stdout is not None
            for raw in process.stdout:
                try:
                    record = json.loads(raw)
                except (ValueError, UnicodeDecodeError):
                    continue
                if not isinstance(record, dict):
                    continue
                kind = record.get("type")
                if kind == "catalog":
                    result = record
                elif kind == "notice":
                    notice = self._auth_notice(record.get("event"))
                    if notice:
                        self.post({"type": "ai_auth_notice", "event": notice})
                elif kind == "prompt":
                    prompt = record.get("prompt")
                    if not isinstance(prompt, dict) or prompt.get("type") not in ("text", "secret", "manual_code", "select"):
                        continue
                    self._auth_request_no += 1
                    ui_id = f"orcad-auth-{self._auth_request_no}"
                    event, response = threading.Event(), {}
                    options = prompt.get("options")
                    safe_options = ([{"id": str(item.get("id", ""))[:200], "label": str(item.get("label", ""))[:200],
                                      "description": str(item.get("description", ""))[:300]}
                                     for item in options[:100] if isinstance(item, dict)]
                                    if prompt.get("type") == "select" and isinstance(options, list) else [])
                    safe_prompt = {"type": prompt["type"], "message": str(prompt.get("message", ""))[:2000],
                                   "placeholder": str(prompt.get("placeholder", ""))[:300], "options": safe_options}
                    with self._lock:
                        self._auth_dialog = (ui_id, event, response, safe_prompt)
                    self.post({"type": "ai_auth_prompt", "id": ui_id, "prompt": safe_prompt})
                    event.wait(600)
                    with self._lock:
                        self._auth_dialog = None
                    if not response:
                        response = {"cancelled": True}
                    helper_response = {"id": record.get("id"), **response}
                    if process.stdin:
                        process.stdin.write(json.dumps(helper_response, ensure_ascii=True).encode("utf-8") + b"\n")
                        process.stdin.flush()
                elif kind == "done":
                    result = {"type": "done", "ok": bool(record.get("ok")),
                              "cancelled": bool(record.get("cancelled"))}
            process.wait(timeout=5)
            return result
        finally:
            with self._lock:
                self._auth_dialog = None
                if self._auth_process is process:
                    self._auth_process = None
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
            if process.stdin:
                process.stdin.close()
            if process.stdout:
                process.stdout.close()

    def _refresh_provider_catalog(self) -> None:
        if self._config.get("source") == "pi" and not self._private_agent_dir().is_dir():
            with self._lock:
                self._providers = []
            return
        try:
            result = self._run_auth_helper("catalog")
            credentials = {item.get("providerId"): item.get("type") for item in result.get("credentials", [])
                           if isinstance(item, dict)}
            providers = []
            for item in result.get("providers", []):
                if not isinstance(item, dict) or not item.get("id"):
                    continue
                credential = credentials.get(item["id"])
                providers.append({"id": str(item["id"]), "name": str(item.get("name", item["id"])),
                                  "methods": [method for method in item.get("methods", [])
                                              if method in ("oauth", "api_key")],
                                  "subscription": bool(item.get("subscription")),
                                  "status": "signed in" if credential == "oauth" else
                                           "key set" if credential == "api_key" or item.get("configured") else "not configured"})
            with self._lock:
                self._providers = providers
        except Exception:
            pass

    def authenticate(self, action: Any, provider_id: Any, auth_type: Any = None) -> bool:
        if action not in ("login", "logout") or not isinstance(provider_id, str) or not re.fullmatch(r"[a-zA-Z0-9._-]{1,100}", provider_id) \
                or (action == "login" and auth_type not in ("oauth", "api_key")):
            return False
        if action == "logout":
            auth_type = ""
        with self._lock:
            if self._config.get("source") == "pi" or self._active_id is not None or self._configuring or self._auth_busy:
                return False
            provider = next((item for item in self._providers if item["id"] == provider_id), None)
            if not provider or (action == "login" and auth_type not in provider["methods"]):
                return False
            self._auth_busy = True
        threading.Thread(target=self._auth_worker, args=(action, provider_id, auth_type),
                         name="orcad-ai-auth", daemon=True).start()
        self.post(self.status())
        return True

    def _auth_worker(self, action: str, provider_id: str, auth_type: str) -> None:
        ok, cancelled = False, False
        try:
            self._load_config()
            self._stop_process()
            result = self._run_auth_helper(action, provider_id, auth_type)
            ok, cancelled = bool(result.get("ok")), bool(result.get("cancelled"))
            self._ensure_process()
            self._refresh_models()
            self._refresh_provider_catalog()
        except Exception:
            pass
        finally:
            with self._lock:
                self._auth_busy = False
            self.post({"type": "ai_auth_done", "action": action, "provider": provider_id,
                       "ok": ok, "cancelled": cancelled,
                       **({"message": "Authentication did not complete; try again."} if not ok and not cancelled else {})})
            self.post(self.status())

    def respond_auth(self, message: dict[str, Any]) -> None:
        with self._lock:
            dialog = self._auth_dialog
            if not dialog or message.get("id") != dialog[0]:
                return
            prompt = dialog[3]
            result = dialog[2]
            if message.get("cancelled") is True:
                result["cancelled"] = True
            else:
                value = message.get("value")
                if not isinstance(value, str) or len(value) > 16384:
                    return
                if prompt["type"] == "select" and value not in {item["id"] for item in prompt["options"]}:
                    return
                result["value"] = value
            dialog[1].set()

    def cancel_auth(self) -> None:
        with self._lock:
            dialog = self._auth_dialog
            process = self._auth_process
            if dialog:
                dialog[2]["cancelled"] = True
                dialog[1].set()
                return
        if process and process.stdin:
            def cancel_worker():
                try:
                    process.stdin.write(b'{"type":"cancel"}\n')
                    process.stdin.flush()
                except (BrokenPipeError, OSError):
                    pass
            threading.Thread(target=cancel_worker, name="orcad-ai-auth-cancel", daemon=True).start()

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
        self.cancel_auth()
        with self._lock:
            auth_process = self._auth_process
        if auth_process and auth_process.poll() is None:
            auth_process.terminate()
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
