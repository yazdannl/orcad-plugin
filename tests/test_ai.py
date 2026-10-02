"""Pi runtime provisioning and RPC bridge tests; the fake speaks real JSONL RPC."""
from __future__ import annotations

import hashlib
import importlib.util
import io
import json
import stat
import sys
import threading
import time
import zipfile
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

import ai.agent as agent_module
import ai.bootstrap as bootstrap
import orcad
from fakes import assert_mode, fake_command


class Response(io.BytesIO):
    def __init__(self, body: bytes, url: str):
        super().__init__(body)
        self.headers = {"Content-Length": str(len(body))}
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()


class Server:
    def __init__(self, body: bytes):
        self.body = body
        self.requests = []

    def __call__(self, request, timeout):
        self.requests.append(request.full_url)
        return Response(self.body, request.full_url)


def fake_node_zip() -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("node-v24.21.0-win-x64/node.exe", b"node-placeholder")
        archive.writestr("node-v24.21.0-win-x64/node_modules/npm/bin/npm-cli.js", b"npm-placeholder")
    return buffer.getvalue()


def test_bundle_contains_the_ai_package_and_render_extension():
    root = Path(__file__).resolve().parents[1]
    spec = importlib.util.spec_from_file_location("bundle_ai", root / "packaging" / "bundle.py")
    bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    with zipfile.ZipFile(io.BytesIO(bundle.backend_archive())) as archive:
        assert {"ai/__init__.py", "ai/bootstrap.py", "ai/agent.py", "ai/providers.py",
                "ai/pi_auth_helper.mjs", "ai/render_openscad.ts"} <= set(archive.namelist())


def test_node_artifacts_are_pinned_for_supported_platforms():
    assert {bootstrap.artifact_for(system, arch).key for system, arch in (
        ("linux", "x86_64"), ("linux", "aarch64"), ("darwin", "x86_64"),
        ("darwin", "arm64"), ("win32", "AMD64"))} == set(bootstrap.NODE_HASHES)
    assert all(len(value) == 64 for value in bootstrap.NODE_HASHES.values())
    assert bootstrap.data_root("/isolated/ai") == Path("/isolated/ai")


def test_bootstrap_download_checksum_atomic_install_and_reuse(tmp_path, monkeypatch):
    payload = fake_node_zip()
    digest = hashlib.sha256(payload).hexdigest()
    monkeypatch.setitem(bootstrap.NODE_HASHES, "win-x64", digest)
    server = Server(payload)
    installs, install_envs = [], []

    def install(argv, env, cancel):
        installs.append(argv)
        install_envs.append(dict(env))
        prefix = Path(argv[argv.index("--prefix") + 1])
        cli = prefix / "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
        cli.parent.mkdir(parents=True)
        cli.write_text("// fake Pi CLI", encoding="utf-8")
        (cli.parents[2] / "package.json").write_text('{"version":"0.87.1"}', encoding="utf-8")

    monkeypatch.setattr(bootstrap, "_run_install", install)
    monkeypatch.setattr(bootstrap, "_verify_pi", lambda root, _node, _env: bootstrap._pi_cli(root))
    monkeypatch.setattr(bootstrap, "child_env", lambda: {
        "ORCAD_CUSTOM_TEST_API_KEY": "dummy-custom-key", "OPENAI_API_KEY": "dummy-openai-key", "PATH": "/usr/bin"})
    progress = []
    paths = bootstrap.ensure_ai(root=tmp_path, system="win32", machine="x86_64", opener=server,
                                progress=lambda message, value: progress.append((message, value)))
    assert Path(paths["node"]).is_file() and Path(paths["pi"]).is_file()
    assert len(installs) == 1 and "--ignore-scripts" in installs[0]
    assert "ORCAD_CUSTOM_TEST_API_KEY" not in install_envs[0] and "OPENAI_API_KEY" not in install_envs[0]
    assert any(message == "Downloading Node.js" for message, _value in progress)
    assert progress[-1] == ("Ready", 1.0)
    assert bootstrap.ensure_ai(root=tmp_path, system="win32", machine="x86_64", opener=server) == paths
    assert len(server.requests) == 1
    assert not list(tmp_path.glob(".install-*"))


def test_download_cancellation_removes_partial_archive(tmp_path):
    artifact = bootstrap.artifact_for("win32", "x64")
    cancel = threading.Event()
    cancel.set()
    destination = tmp_path / artifact.filename
    with pytest.raises(InterruptedError, match="cancelled"):
        bootstrap._download(artifact, destination, Server(b"archive"), cancel, lambda *_args: None)
    assert not destination.exists() and not destination.with_name(destination.name + ".part").exists()


def test_bootstrap_rejects_bad_digest_and_cleans_partial_install(tmp_path, monkeypatch):
    monkeypatch.setitem(bootstrap.NODE_HASHES, "win-x64", "0" * 64)
    with pytest.raises(ValueError, match="checksum"):
        bootstrap.ensure_ai(root=tmp_path, system="win32", machine="x86_64", opener=Server(fake_node_zip()))
    assert not list(tmp_path.glob("runtime-*"))
    assert not list(tmp_path.glob(".install-*"))


def _fake_pi(tmp_path: Path, mode: str) -> tuple[Path, Path]:
    pi_root = tmp_path / "fake-install/pi/node_modules/@earendil-works/pi-coding-agent"
    cli = pi_root / "dist/bundle/cli.js"
    cli.parent.mkdir(parents=True)
    cli.write_text("// ignored by fake RPC", encoding="utf-8")
    script = tmp_path / "fake-pi.py"
    script.write_text(
        "import json, os, pathlib, sys, time\n"
        f"mode = {mode!r}\n"
        "def emit(value): print(json.dumps(value), flush=True)\n"
        "for line in sys.stdin:\n"
        "    command = json.loads(line)\n"
        "    kind = command.get('type')\n"
        "    if kind == 'get_available_models':\n"
        "        emit({'id': command.get('id'), 'type': 'response', 'command': kind, 'success': True, 'data': {'models': [{'provider':'openai','id':'test-model','name':'Test'}]}})\n"
        "    elif kind == 'prompt':\n"
        "        emit({'id': command.get('id'), 'type': 'response', 'command': kind, 'success': True})\n"
        "        emit({'type':'message_update','assistantMessageEvent':{'type':'text_delta','delta':'Working'}})\n"
        "        emit({'type':'message_update','assistantMessageEvent':{'type':'thinking_delta','delta':'Checking geometry'}})\n"
        "        if mode == 'mapping':\n"
        "            pathlib.Path('model.scad').write_text('cube([20,20,20]);\\n')\n"
        "            emit({'type':'tool_execution_start','toolCallId':'call-1','toolName':'write','args':{'path':'model.scad'}})\n"
        "            emit({'type':'tool_execution_end','toolCallId':'call-1','toolName':'write','result':{'content':[]},'isError':False})\n"
        "            emit({'type':'agent_settled'})\n"
        "    elif kind == 'abort':\n"
        "        emit({'id':command.get('id'),'type':'response','command':kind,'success':True})\n"
        "        emit({'type':'agent_settled'})\n"
        "    elif kind == 'new_session':\n"
        "        emit({'id':command.get('id'),'type':'response','command':kind,'success':True})\n",
        encoding="utf-8")
    return script, cli


def _agent(tmp_path, monkeypatch, mode="mapping"):
    script, cli = _fake_pi(tmp_path, mode)
    openscad = fake_command(tmp_path, "fake-openscad", "print('OpenSCAD version 2026.09.22')\n")
    monkeypatch.setattr(bootstrap, "start_ai_bootstrap", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(bootstrap, "wait_for_ai", lambda **_kwargs: {"node": sys.executable, "pi": str(cli)})
    monkeypatch.setattr(bootstrap, "_STATE", {"state": "missing", "node": None, "pi": None,
                                               "message": None, "progress": None})
    messages = []
    instance = agent_module.PiAgent(messages.append, root=tmp_path / "data", library=tmp_path,
                                    openscad=lambda: str(openscad), child_env=lambda: {},
                                    command_factory=lambda _config: [sys.executable, str(script)])
    return instance, messages


def _wait_for(messages, predicate, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        found = next((message for message in messages if predicate(message)), None)
        if found is not None:
            return found
        time.sleep(0.01)
    raise AssertionError(f"timed out waiting for RPC event; got {messages!r}")


def test_rpc_events_map_text_thinking_tool_and_changed_code(tmp_path, monkeypatch):
    instance, messages = _agent(tmp_path, monkeypatch)
    assert instance.prompt("run-1", "make a cube", "// initial")
    done = _wait_for(messages, lambda item: item.get("type") == "ai_done")
    assert done["id"] == "run-1" and done["ok"] and "cube([20,20,20])" in done["code"]
    assert any(item.get("kind") == "text" and item.get("delta") == "Working" for item in messages)
    assert any(item.get("kind") == "thinking" for item in messages)
    assert [(item.get("phase"), item.get("name")) for item in messages if item.get("kind") == "tool"] == [
        ("start", "write"), ("end", "write")]
    assert any(item.get("type") == "ai_code" and "cube([20,20,20])" in item["code"] for item in messages)
    instance.close()


def test_configuration_is_reported_busy_until_async_update_finishes(tmp_path, monkeypatch):
    instance, _messages = _agent(tmp_path, monkeypatch)
    instance._configure_lock.acquire()
    instance.configure({"type": "ai_config", "source": "pi"})
    assert instance.status()["busy"]
    assert not instance.prompt("while-configuring", "prompt", "")
    instance._configure_lock.release()
    deadline = time.monotonic() + 3
    while instance.status()["busy"] and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not instance.status()["busy"]
    instance.close()


def test_rpc_busy_and_abort(tmp_path, monkeypatch):
    instance, messages = _agent(tmp_path, monkeypatch, "wait")
    assert instance.prompt("run-1", "wait", "cube(1);")
    _wait_for(messages, lambda item: item.get("kind") == "text")
    assert not instance.prompt("run-2", "busy", "")
    instance.abort("run-1")
    done = _wait_for(messages, lambda item: item.get("type") == "ai_done")
    assert done["ok"] is False and done["error"] == "cancelled"
    instance.close()


def test_session_routes_ai_protocol_and_returns_busy():
    session = orcad.Session(lambda _message: None)

    class FakeAI:
        def status(self):
            return {"type": "ai_status", "state": "ready"}
        def setup(self):
            pass
        def configure(self, _message):
            pass
        def prompt(self, _run_id, _text, _code):
            return False
        def abort(self, _run_id):
            pass
        def reset(self):
            pass
        def authenticate(self, *_args):
            return True
        def respond_auth(self, _message):
            pass
        def cancel_auth(self):
            pass
        def detect_custom_models(self, _message):
            return True
        def save_custom_provider(self, _message):
            return True
        def remove_custom_provider(self, _message):
            return True
        def close(self):
            pass

    session._ai = FakeAI()
    assert session.handle({"type": "ai_status"}) == {"type": "ai_status", "state": "ready"}
    assert session.handle({"type": "ai_setup"})["state"] == "ready"
    assert session.handle({"type": "ai_config", "source": "pi"})["state"] == "ready"
    assert session.handle({"type": "ai_auth", "action": "login", "provider": "github-copilot", "auth_type": "oauth"}) is None
    assert session.handle({"type": "ai_auth_response", "id": "dialog", "value": "one-time-value"}) is None
    assert session.handle({"type": "ai_auth_cancel"}) is None
    assert session.handle({"type": "ai_provider_detect", "id": "detect", "baseUrl": "http://localhost"}) is None
    assert session.handle({"type": "ai_provider_save", "provider": {"name": "Test"}}) is None
    assert session.handle({"type": "ai_provider_remove", "id": "test"}) is None
    busy = session.handle({"type": "ai_prompt", "id": "x", "text": "edit", "code": ""})
    assert busy == {"type": "ai_done", "id": "x", "ok": False, "error": "busy", "code": ""}
    assert session.handle({"type": "ai_abort", "id": "x"}) is None
    assert session.handle({"type": "ai_reset"}) is None
    session.shutdown()


def test_sdk_auth_prompts_bridge_without_echoing_secret(tmp_path, monkeypatch):
    instance, messages = _agent(tmp_path, monkeypatch)
    script = tmp_path / "fake-auth.py"
    script.write_text(
        """import json, sys
print(json.dumps({'type':'notice','event':{'type':'device_code','userCode':'DEMO-CODE','verificationUri':'https://example.test/device'}}), flush=True)
print(json.dumps({'type':'prompt','id':'auth-prompt','prompt':{'type':'secret','message':'API key'}}), flush=True)
response = json.loads(sys.stdin.readline())
print(json.dumps({'type':'done','ok':response.get('value')=='test-placeholder'}), flush=True)
""",
        encoding="utf-8")
    instance._config["source"] = "managed"
    instance.auth_helper_factory = lambda *_args: [sys.executable, str(script)]
    messages.clear()
    def post(message):
        messages.append(message)
        if message.get("type") == "ai_auth_prompt":
            instance.respond_auth({"id": message["id"], "value": "test-placeholder"})
    instance.post = post
    result = instance._run_auth_helper("login", "test-provider", "api_key")
    assert result["ok"]
    assert any(message.get("type") == "ai_auth_prompt" for message in messages)
    notice = next(message for message in messages if message.get("type") == "ai_auth_notice")
    assert notice["event"]["userCode"] == "DEMO-CODE"
    assert all("test-placeholder" not in json.dumps(message) for message in messages)
    instance.close()


def test_sign_out_routes_to_pi_auth_storage(tmp_path, monkeypatch):
    instance, messages = _agent(tmp_path, monkeypatch)
    script = tmp_path / "fake-logout.py"
    script.write_text("import json; print(json.dumps({'type':'done','ok':True}), flush=True)\n", encoding="utf-8")
    instance._config["source"] = "managed"
    instance._providers = [{"id": "openai", "name": "OpenAI", "methods": ["api_key"], "status": "key set"}]
    instance._stop_process = lambda: None
    instance._ensure_process = lambda: None
    instance._refresh_models = lambda: None
    instance._refresh_provider_catalog = lambda: None
    captured = []
    instance.auth_helper_factory = lambda *args: (captured.append(args), [sys.executable, str(script)])[1]
    assert instance.authenticate("logout", "openai")
    done = _wait_for(messages, lambda item: item.get("type") == "ai_auth_done")
    assert done["ok"] and captured[0][3:] == ("logout", "openai", "")
    instance.close()


def test_custom_provider_models_json_and_key_permissions(tmp_path, monkeypatch):
    instance, messages = _agent(tmp_path, monkeypatch)
    instance._config["source"] = "managed"
    assert instance.save_custom_provider({"provider": {
        "name": "Local Test", "baseUrl": "http://127.0.0.1:11434/v1", "api": "openai-completions",
        "models": [{"id": "qwen-local", "name": "Qwen local"}],
    }, "api_key": "test-local-key"})
    saved = _wait_for(messages, lambda item: item.get("type") == "ai_provider_result" and item.get("action") == "save")
    assert saved["ok"] and saved["id"] == "local-test"
    private = tmp_path / "data/private-agent"
    models_path = private / "models.json"
    models = json.loads(models_path.read_text(encoding="utf-8"))
    assert_mode(models_path, 0o600)
    assert_mode(private, 0o700)
    assert models["providers"]["local-test"]["models"] == [{"id": "qwen-local", "name": "Qwen local"}]
    assert models["providers"]["local-test"]["apiKey"] == "${ORCAD_CUSTOM_LOCAL_TEST_API_KEY}"
    assert "test-local-key" not in models_path.read_text(encoding="utf-8")
    key_path = private / "custom-keys/local-test.key"
    assert_mode(key_path, 0o600)
    assert_mode(private / "custom-keys", 0o700)
    assert instance.status()["custom_providers"][0]["has_key"]
    child = agent_module._clean_pi_environment(
        {}, {**instance._config, "node": sys.executable, "openscad": "openscad", "library": str(tmp_path)},
        tmp_path / "data")
    assert child["ORCAD_CUSTOM_LOCAL_TEST_API_KEY"] == "test-local-key"
    assert instance.remove_custom_provider({"id": "local-test"})
    removed = _wait_for(messages, lambda item: item.get("type") == "ai_provider_result" and item.get("action") == "remove")
    assert removed["ok"]
    assert json.loads(models_path.read_text(encoding="utf-8")) == {"providers": {}}
    assert not key_path.exists()
    instance.close()


def test_detect_models_uses_bounded_openai_style_endpoint_and_key(tmp_path):
    received = {}
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            received["path"] = self.path
            received["authorization"] = self.headers.get("Authorization")
            payload = json.dumps({"data": [{"id": "dummy-model"}]}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        def log_message(self, *_args):
            pass
    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        endpoint = f"http://127.0.0.1:{server.server_port}/v1"
        assert agent_module.custom_provider_config.detect_models(endpoint, "test-discovery-key") == [
            {"id": "dummy-model", "name": "dummy-model"}]
        assert received == {"path": "/v1/models", "authorization": "Bearer test-discovery-key"}
    finally:
        server.shutdown()
        thread.join(timeout=2)
        server.server_close()
    with pytest.raises(ValueError, match="HTTPS"):
        agent_module.custom_provider_config.validate_base_url("http://example.com/v1")


def test_private_key_config_is_0600_and_never_echoed(tmp_path, monkeypatch):
    instance, messages = _agent(tmp_path, monkeypatch)
    instance._configure_worker("key", "openai", "model-id", "low", "dummy-test-value")
    keyfile = tmp_path / "data/provider-key"
    assert_mode(keyfile, 0o600)
    status = instance.status()
    assert status["config"]["source"] == "managed" and status["config"]["has_key"]
    child = agent_module._clean_pi_environment(
        {}, {**instance._config, "node": sys.executable, "openscad": "openscad", "library": str(tmp_path)},
        tmp_path / "data")
    assert "OPENAI_API_KEY" in child and child["PI_CODING_AGENT_DIR"] == str(tmp_path / "data/private-agent")
    switched = agent_module._clean_pi_environment(
        {}, {**instance._config, "provider": "anthropic", "node": sys.executable,
            "openscad": "openscad", "library": str(tmp_path)}, tmp_path / "data")
    assert "OPENAI_API_KEY" not in switched and "ANTHROPIC_API_KEY" not in switched
    assert all("dummy-test-value" not in json.dumps(message) for message in messages)
    instance.close()
