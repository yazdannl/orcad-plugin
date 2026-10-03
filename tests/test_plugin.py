"""Plugin entry point: message protocol, render queue, exports and handoff."""
from __future__ import annotations

import base64
import ctypes
import hashlib
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import types
from array import array
from pathlib import Path

import pytest

import orcad
from test_backend import stl

scad = orcad.scad


class FakeRunner:
    def __init__(self, tmp_path, gate=None):
        self.engine = scad.EngineInfo("openscad", "2026.09.22", True)
        self.path = tmp_path / "model.stl"
        self.path.write_bytes(stl())
        self.gate = gate
        self.calls = []

    def _result(self):
        return scad.RenderResult(self.path, False, 12, ["ECHO: 1"], 12)

    def render_object(self, name, params, quality, cancel=None):
        self.calls.append((name, params, quality))
        if self.gate and name == "box":
            self.gate.wait(5)
        scad.validate_parameters(name, params)
        return self._result()

    def render_code(self, code, quality, cancel=None):
        self.calls.append(("code", code, quality))
        if "bad" in code:
            raise scad.BackendError(scad.ErrorCode.PROCESS_FAILED, "Parser error on line 1", {"line": 1})
        return self._result()


@pytest.fixture
def session(tmp_path, monkeypatch):
    monkeypatch.setattr(orcad, "EXPORTS_DIR", tmp_path / "exports")
    posted = []
    s = orcad.Session(posted.append)
    s._runner = FakeRunner(tmp_path)
    s.posted = posted
    return s


def results(session, count, timeout=5):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        done = [m for m in session.posted if m["type"] == "result"]
        if len(done) >= count:
            return done
        time.sleep(0.01)
    raise AssertionError(f"expected {count} results, got {session.posted}")


def test_hello_engine_and_malformed_messages(session):
    hello = session.handle({"type": "hello"})
    assert hello["version"] == orcad.PLUGIN_VERSION and hello["engine"]["state"] == "ready"
    assert session.handle('{"type": "engine"}')["version"] == "2026.09.22"
    assert session.handle("not json")["type"] == "error"
    assert session.handle({"type": "fly"})["type"] == "error"


@pytest.mark.parametrize("msg", [
    {"type": "render", "id": "1", "object": "box"},
    {"type": "render", "id": 1, "object": "box", "quality": "ultra"},
    {"type": "render", "id": 1, "object": "box", "format": "step", "purpose": "export"},
    {"type": "render", "id": 1, "object": "nope"},
    {"type": "render", "id": 1},
    {"type": "render", "id": 1, "object": "box", "purpose": "print"},
])
def test_bad_render_requests_fail_immediately(session, msg):
    reply = session.handle(msg)
    assert reply["type"] == "result" and reply["ok"] is False and reply["error"]


def test_preview_returns_a_compact_indexed_mesh(session):
    session.handle({"type": "render", "id": 7, "purpose": "preview", "object": "box", "params": {"length": 30}})
    (result,) = results(session, 1)
    assert result["ok"] and result["id"] == 7 and result["triangles"] == 12 and result["log"] == ["ECHO: 1"]
    mesh = result["mesh"]
    assert mesh["index_type"] == "uint16"
    positions = array("f", base64.b64decode(mesh["positions"]))
    indices = array("H", base64.b64decode(mesh["indices"]))
    assert len(positions) == 24 and len(indices) == 36 and max(indices) == 7
    assert any(m["type"] == "progress" and m["id"] == 7 for m in session.posted)


def test_newest_preview_wins_and_exports_are_never_dropped(session, tmp_path):
    gate = threading.Event()
    session._runner = FakeRunner(tmp_path, gate)
    session.handle({"type": "render", "id": 1, "purpose": "preview", "object": "box"})
    time.sleep(0.1)  # id 1 is now rendering and blocked
    session.handle({"type": "render", "id": 2, "purpose": "preview", "object": "cylinder"})
    session.handle({"type": "render", "id": 3, "purpose": "preview", "object": "tube"})
    session.handle({"type": "render", "id": 4, "purpose": "export", "object": "cylinder", "format": "3mf", "name": "My part!"})
    gate.set()
    done = results(session, 2)
    time.sleep(0.2)
    assert [m["id"] for m in session.posted if m["type"] == "result"] == [4, 3]
    export = done[0]
    assert export["format"] == "3mf" and Path(export["file"]).name.startswith("My_part_")
    assert Path(export["file"]).is_file() and export["size_bytes"] > 0


def test_cancel_discards_a_queued_job(session, tmp_path):
    gate = threading.Event()
    session._runner = FakeRunner(tmp_path, gate)
    session.handle({"type": "render", "id": 1, "purpose": "export", "object": "box"})
    session.handle({"type": "render", "id": 2, "purpose": "export", "object": "tube"})
    session.handle({"type": "cancel", "id": 2})
    gate.set()
    results(session, 1)
    time.sleep(0.2)
    assert [m["id"] for m in session.posted if m["type"] == "result"] == [1]


def test_failures_carry_fields_and_code_lines(session):
    session.handle({"type": "render", "id": 1, "purpose": "preview", "object": "tube", "params": {"inner_diameter": 40}})
    session.handle({"type": "render", "id": 2, "purpose": "export", "code": "bad("})
    first, second = sorted(results(session, 2), key=lambda m: m["id"])
    assert first["ok"] is False and set(first["fields"]) == {"inner_diameter", "outer_diameter"}
    assert second["ok"] is False and second["line"] == 1 and second["error_code"] == "process_failed"


def test_plate_exports_stl_and_hands_it_to_orca(session, monkeypatch):
    sent = []
    monkeypatch.setattr(orcad, "send_to_orca", lambda path: sent.append(path) or {"ok": True, "message": "sent"})
    session.handle({"type": "render", "id": 5, "purpose": "plate", "object": "box", "format": "3mf"})
    (result,) = results(session, 1)
    assert result["format"] == "stl" and result["handoff"] == {"ok": True, "message": "sent"}
    assert sent == [Path(result["file"])]


def test_windows_copydata_runs_on_render_worker(session, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    workers = []
    monkeypatch.setattr(orcad, "_windows_copydata_handoff",
                        lambda path: workers.append(threading.current_thread()) or (True, ""))
    session.handle({"type": "render", "id": 6, "purpose": "plate", "object": "box"})
    (result,) = results(session, 1)
    assert result["handoff"]["ok"]
    assert workers and workers[0] is not threading.main_thread()


def test_engine_setup_progress_is_reported(tmp_path, monkeypatch):
    states = iter([{"state": "starting", "progress": 0.5}] * 2 + [{"state": "ready", "path": "/x", "progress": None}])
    monkeypatch.setattr(scad, "start_openscad_bootstrap", lambda: None)
    monkeypatch.setattr(scad, "openscad_bootstrap_status", lambda: next(states))
    monkeypatch.setattr(scad, "OpenSCADRunner", lambda exe, cache_dir: FakeRunner(tmp_path))
    posted = []
    s = orcad.Session(posted.append)
    s.handle({"type": "render", "id": 1, "object": "box"})
    s.posted = posted
    results(s, 1)
    assert any(m["type"] == "progress" and m.get("progress") == 0.5 and "50%" in m["message"] for m in posted)
    assert any(m["type"] == "engine" and m["state"] == "ready" for m in posted)


def test_large_meshes_switch_to_32_bit_indices(monkeypatch):
    mesh = scad.IndexedMesh(array("f", [0.0] * 3 * 70000), array("I", [0, 1, 69999]))
    monkeypatch.setattr(scad, "index_stl", lambda data: mesh)
    assert orcad.mesh_payload(b"")["index_type"] == "uint32"


@pytest.mark.parametrize("platform, exe, expected", [
    ("linux", "/opt/orca/bin/orca-slicer", ["/opt/orca/bin/orca-slicer"]),
    ("win32", r"C:\Program Files\OrcaSlicer\OrcaSlicer.exe", [r"C:\Program Files\OrcaSlicer\OrcaSlicer.exe"]),
    ("darwin", "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer", ["open", "-a", "/Applications/OrcaSlicer.app"]),
    ("linux", "/usr/bin/python3", None),
    ("win32", "C:/OrcaSlicer/python.exe", None),
])
def test_handoff_targets_the_running_orcaslicer(monkeypatch, platform, exe, expected):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(orcad, "_host_executable", lambda: exe)
    command = orca_cmd()
    if platform == "darwin":  # the command is built with Path(), which uses the host separator
        command = [part.replace("\\", "/") for part in command]
    assert command == expected


def orca_cmd():
    return orcad.orca_open_command()


def test_send_to_orca_reports_launch_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(orcad, "orca_open_command", lambda: ["/missing/orca-slicer"])
    assert orcad.send_to_orca(tmp_path / "x.stl")["ok"] is False
    class Child:
        @staticmethod
        def wait(timeout):
            return 0

    launched = []
    monkeypatch.setattr(orcad.subprocess, "Popen", lambda argv, **kw: launched.append(argv) or Child())
    assert orcad.send_to_orca(tmp_path / "x.stl")["ok"] is True
    assert launched == [["/missing/orca-slicer", str(tmp_path / "x.stl")]]


def test_windows_fallback_uses_hidden_child_and_checks_exit(monkeypatch, tmp_path):
    class Child:
        def wait(self, timeout):
            assert timeout == 2
            return 0

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(orcad, "_windows_copydata_handoff", lambda path: (False, "mocked IPC miss"))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(orcad, "orca_open_command", lambda: [r"C:\Orca\OrcaSlicer.exe"])
    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: launched.append((argv, kw)) or Child())
    result = orcad.send_to_orca(tmp_path / "part.stl")
    assert result["ok"] and "exited successfully" in result["message"]
    argv, options = launched[0]
    assert argv == [r"C:\Orca\OrcaSlicer.exe", str(tmp_path / "part.stl")]
    assert options["creationflags"] == 0x08000000 and options["close_fds"] is True
    assert "start_new_session" not in options


def test_windows_fallback_reports_timeout_and_nonzero_exit(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(orcad, "_windows_copydata_handoff", lambda path: (False, "mocked IPC miss"))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(orcad, "orca_open_command", lambda: [r"C:\Orca\OrcaSlicer.exe"])

    class Child:
        def __init__(self, result):
            self.result = result

        def wait(self, timeout):
            if isinstance(self.result, Exception):
                raise self.result
            return self.result

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Child(subprocess.TimeoutExpired("orca", 2)))
    result = orcad.send_to_orca(tmp_path / "part.stl")
    assert result["ok"] and "launch is still running" in result["message"]
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Child(7))
    result = orcad.send_to_orca(tmp_path / "part.stl")
    assert not result["ok"] and "status 7" in result["message"]


@pytest.mark.parametrize("exit_code", [-1, 0xFFFFFFFF])
def test_windows_fallback_accepts_orcas_single_instance_exit(monkeypatch, tmp_path, exit_code):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(orcad, "_windows_copydata_handoff", lambda path: (False, "mocked IPC miss"))
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    monkeypatch.setattr(orcad, "orca_open_command", lambda: [r"C:\Orca\OrcaSlicer.exe"])

    class Child:
        def wait(self, timeout):
            assert timeout == 2
            return exit_code

    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: Child())
    result = orcad.send_to_orca(tmp_path / "part.stl")
    assert result["ok"] and "single-instance forwarding path" in result["message"]


def test_windows_instance_payload_matches_orcas_c_style_argv(monkeypatch):
    monkeypatch.setattr(orcad, "_host_executable", lambda: r"C:\Orca\OrcaSlicer.exe")
    path = Path('C:\\Grid Models\\part; "A".stl')
    expected = r'"C:\\Orca\\OrcaSlicer.exe";"C:\\Grid Models\\part; \"A\".stl"'
    assert orcad._windows_instance_payload(path) == expected
    unicode_path = Path('C:\\模型\\part.stl')
    assert '模型' in orcad._windows_instance_payload(unicode_path)


def fake_windows_user32(monkeypatch, target_pid=None):
    from ctypes import wintypes

    calls = {}

    class CopyData(ctypes.Structure):
        _fields_ = [("dwData", ctypes.c_size_t), ("cbData", wintypes.DWORD), ("lpData", ctypes.c_void_p)]

    class User32:
        @staticmethod
        def GetClassNameW(hwnd, buffer, size):
            buffer.value = "wxWindowNR"
            return len(buffer.value)

        @staticmethod
        def GetPropW(hwnd, name):
            return 1

        @staticmethod
        def GetWindowThreadProcessId(hwnd, pid_pointer):
            pid = ctypes.cast(pid_pointer, ctypes.POINTER(wintypes.DWORD))
            pid.contents.value = target_pid if target_pid is not None else os.getpid()
            return 1

        @staticmethod
        def EnumWindows(callback, _lparam):
            callback(0x1234, 0)
            return 1

        @staticmethod
        def SendMessageW(hwnd, message, wparam, lparam):
            data = ctypes.cast(lparam, ctypes.POINTER(CopyData)).contents
            calls["window"] = hwnd
            calls["message"] = message
            calls["wparam"] = wparam
            calls["dwData"] = data.dwData
            calls["cbData"] = data.cbData
            calls["payload"] = ctypes.wstring_at(data.lpData)
            return 1  # GUI_App's WM_COPYDATA handler returns TRUE after queuing the file event.

    user32 = User32()
    monkeypatch.setattr(ctypes, "WinDLL", lambda name, **kwargs: user32, raising=False)
    monkeypatch.setattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE, raising=False)
    return calls


def test_windows_copydata_is_primary_with_mocked_orca_module(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    fake_orca = types.ModuleType("orca")
    fake_orca.host = types.SimpleNamespace(plater=lambda: types.SimpleNamespace(model=lambda: None))
    assert not hasattr(fake_orca.host.plater(), "load_files")
    monkeypatch.setattr(orcad, "orca", fake_orca)
    calls = fake_windows_user32(monkeypatch)
    monkeypatch.setattr(orcad, "orca_open_command", lambda: pytest.fail("process fallback should not run"))
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: pytest.fail("process fallback should not run"))

    path = Path(r"C:\Grid Models\part.stl")
    result = orcad.send_to_orca(path)
    assert result["ok"] and "running OrcaSlicer window" in result["message"]
    payload = orcad._windows_instance_payload(path)
    # Windows wchar_t is UTF-16; include the trailing WCHAR NUL in cbData.
    expected_bytes = (len(payload.encode("utf-16-le")) + 2 if ctypes.sizeof(ctypes.c_wchar) == 2
                      else ctypes.sizeof(ctypes.create_unicode_buffer(payload)))
    assert calls == {"window": 0x1234, "message": 0x004A, "wparam": 0,
                     "dwData": 1, "cbData": expected_bytes, "payload": payload}


def test_windows_copydata_miss_uses_process_fallback(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(subprocess, "CREATE_NO_WINDOW", 0x08000000, raising=False)
    fake_windows_user32(monkeypatch, target_pid=os.getpid() + 1)
    monkeypatch.setattr(orcad, "orca_open_command", lambda: [r"C:\Orca\OrcaSlicer.exe"])

    class Child:
        @staticmethod
        def wait(timeout):
            return 0

    launched = []
    monkeypatch.setattr(subprocess, "Popen", lambda argv, **kw: launched.append(argv) or Child())
    result = orcad.send_to_orca(tmp_path / "part.stl")
    assert result["ok"] and "no OrcaSlicer main window found" in result["message"]
    assert launched == [[r"C:\Orca\OrcaSlicer.exe", str(tmp_path / "part.stl")]]


def test_windows_host_executable_is_the_process_image(monkeypatch):
    class Kernel32:
        @staticmethod
        def GetModuleFileNameW(module, buffer, size):
            assert module is None and size == 32768
            buffer.value = r"C:\Program Files\OrcaSlicer\OrcaSlicer.exe"
            return len(buffer.value)

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(ctypes, "windll", type("WinDLL", (), {"kernel32": Kernel32})(), raising=False)
    assert orcad._host_executable().endswith("OrcaSlicer.exe")
    assert orcad.orca_open_command() == [r"C:\Program Files\OrcaSlicer\OrcaSlicer.exe"]


def test_export_names_are_safe_and_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(orcad, "EXPORTS_DIR", tmp_path)
    first = orcad.export_path("../../etc/passwd bin", "stl")
    first.touch()
    second = orcad.export_path("../../etc/passwd bin", "stl")
    assert first.parent == tmp_path and first.name.startswith("etc_passwd_bin_") and second != first


def test_single_file_install_uses_the_embedded_page_and_backend(tmp_path, monkeypatch):
    if not orcad._EMBEDDED_BACKEND or not orcad._EMBEDDED_FRONTEND:
        pytest.skip("run packaging/bundle.py to embed the release blobs")
    shutil.copy(Path(orcad.__file__), tmp_path / "orcad.py")
    cache = tmp_path / "cache-home"
    monkeypatch.setenv("LOCALAPPDATA", str(cache))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    code = ("import orcad, json; s = orcad.Session(print); "
            "print(json.dumps([orcad.scad.__file__, orcad.page_html()[:15], sorted(orcad.scad.CATALOG['objects'])]))")
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    module_path, page, objects = json.loads(out.strip().splitlines()[-1])
    # unpacked into the short per-user cache, not next to the plugin: the cloud install
    # directory is already ~160 characters, and the vendored paths are 93 more
    assert Path(module_path).is_relative_to(cache / "orcad" / "backend")
    assert page.startswith("<!doctype html>") and "gridfinity_bin" in objects


def test_blocked_rename_still_loads_the_backend_from_the_staging_tree(tmp_path, monkeypatch):
    if not orcad._EMBEDDED_BACKEND:
        pytest.skip("run packaging/bundle.py to embed the release blobs")
    shutil.copy(Path(orcad.__file__), tmp_path / "orcad.py")
    cache = tmp_path / "cache-home"
    monkeypatch.setenv("LOCALAPPDATA", str(cache))
    monkeypatch.setenv("XDG_CACHE_HOME", str(cache))
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    digest = hashlib.sha256(base64.b64decode(orcad._EMBEDDED_BACKEND)).hexdigest()[:16]
    blocked = cache / "orcad" / "backend" / digest
    blocked.mkdir(parents=True)
    (blocked / "in-the-way").write_text("x")  # rename() cannot replace a non-empty directory
    result = subprocess.run([sys.executable, "-c", "import orcad; print(orcad.scad.__file__)"],
                            cwd=tmp_path, capture_output=True, text=True)
    assert result.returncode == 0, result.stderr
    loaded = Path(result.stdout.strip())
    assert loaded.parent.parent.name.endswith(".tmp") and loaded.is_relative_to(blocked.parent)
    assert (blocked / "in-the-way").is_file()


def test_unpackable_backend_reports_the_target_path(monkeypatch, tmp_path):
    monkeypatch.setattr(orcad, "HERE", tmp_path / "plugin")
    monkeypatch.setattr(orcad, "_cache_root", lambda: tmp_path / "cache")

    def too_long(self, path):
        raise OSError(206, "The filename or extension is too long")

    monkeypatch.setattr(orcad.zipfile.ZipFile, "extractall", too_long)
    with pytest.raises(RuntimeError, match="could not unpack the embedded backend"):
        orcad._load_backend()
