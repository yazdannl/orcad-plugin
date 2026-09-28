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
from pathlib import Path

import pytest

import ai.agent as agent_module
import ai.bootstrap as bootstrap
import orcad


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
        assert {"ai/__init__.py", "ai/bootstrap.py", "ai/agent.py", "ai/render_openscad.ts"} <= set(archive.namelist())


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
    installs = []

    def install(argv, _env, cancel):
        installs.append(argv)
        prefix = Path(argv[argv.index("--prefix") + 1])
        cli = prefix / "node_modules/@earendil-works/pi-coding-agent/dist/bundle/cli.js"
        cli.parent.mkdir(parents=True)
        cli.write_text("// fake Pi CLI", encoding="utf-8")
        (cli.parents[2] / "package.json").write_text('{"version":"0.87.1"}', encoding="utf-8")

    monkeypatch.setattr(bootstrap, "_run_install", install)
    monkeypatch.setattr(bootstrap, "_verify_pi", lambda root, _node, _env: bootstrap._pi_cli(root))
    progress = []
    paths = bootstrap.ensure_ai(root=tmp_path, system="win32", machine="x86_64", opener=server,
                                progress=lambda message, value: progress.append((message, value)))
    assert Path(paths["node"]).is_file() and Path(paths["pi"]).is_file()
    assert len(installs) == 1 and "--ignore-scripts" in installs[0]
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
    openscad = tmp_path / "fake-openscad"
    openscad.write_text(f"#!{sys.executable}\nprint('OpenSCAD version 2026.09.22')\n", encoding="utf-8")
    openscad.chmod(openscad.stat().st_mode | stat.S_IXUSR)
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
        def close(self):
            pass

    session._ai = FakeAI()
    assert session.handle({"type": "ai_status"}) == {"type": "ai_status", "state": "ready"}
    assert session.handle({"type": "ai_setup"})["state"] == "ready"
    assert session.handle({"type": "ai_config", "source": "pi"})["state"] == "ready"
    busy = session.handle({"type": "ai_prompt", "id": "x", "text": "edit", "code": ""})
    assert busy == {"type": "ai_done", "id": "x", "ok": False, "error": "busy", "code": ""}
    assert session.handle({"type": "ai_abort", "id": "x"}) is None
    assert session.handle({"type": "ai_reset"}) is None
    session.shutdown()


def test_private_key_config_is_0600_and_never_echoed(tmp_path, monkeypatch):
    instance, messages = _agent(tmp_path, monkeypatch)
    instance._configure_worker("key", "openai", "model-id", "low", "dummy-test-value")
    keyfile = tmp_path / "data/provider-key"
    assert stat.S_IMODE(keyfile.stat().st_mode) == 0o600
    status = instance.status()
    assert status["config"]["source"] == "key" and status["config"]["has_key"]
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
