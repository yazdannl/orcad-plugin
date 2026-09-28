"""Plugin entry point: message protocol, render queue, exports and handoff."""
from __future__ import annotations

import base64
import shutil
import subprocess
import sys
import threading
import time
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
    ("linux", "/opt/orca/bin/orca-slicer", ["/opt/orca/bin/orca-slicer", "--single-instance"]),
    ("win32", r"C:\Program Files\OrcaSlicer\orca-slicer.exe", [r"C:\Program Files\OrcaSlicer\orca-slicer.exe", "--single-instance"]),
    ("darwin", "/Applications/OrcaSlicer.app/Contents/MacOS/OrcaSlicer", ["open", "-a", "/Applications/OrcaSlicer.app"]),
    ("linux", "/usr/bin/python3", None),
])
def test_handoff_targets_the_running_orcaslicer(monkeypatch, platform, exe, expected):
    monkeypatch.setattr(sys, "platform", platform)
    monkeypatch.setattr(orcad, "_host_executable", lambda: exe)
    assert orca_cmd() == expected


def orca_cmd():
    return orcad.orca_open_command()


def test_send_to_orca_reports_launch_failures(monkeypatch, tmp_path):
    monkeypatch.setattr(orcad, "orca_open_command", lambda: ["/missing/orca-slicer", "--single-instance"])
    assert orcad.send_to_orca(tmp_path / "x.stl")["ok"] is False
    launched = []
    monkeypatch.setattr(orcad.subprocess, "Popen", lambda argv, **kw: launched.append(argv))
    assert orcad.send_to_orca(tmp_path / "x.stl")["ok"] is True
    assert launched == [["/missing/orca-slicer", "--single-instance", str(tmp_path / "x.stl")]]


def test_export_names_are_safe_and_unique(tmp_path, monkeypatch):
    monkeypatch.setattr(orcad, "EXPORTS_DIR", tmp_path)
    first = orcad.export_path("../../etc/passwd bin", "stl")
    first.touch()
    second = orcad.export_path("../../etc/passwd bin", "stl")
    assert first.parent == tmp_path and first.name.startswith("etc_passwd_bin_") and second != first


def test_single_file_install_uses_the_embedded_page_and_backend(tmp_path):
    if not orcad._EMBEDDED_BACKEND or not orcad._EMBEDDED_FRONTEND:
        pytest.skip("run packaging/bundle.py to embed the release blobs")
    shutil.copy(Path(orcad.__file__), tmp_path / "orcad.py")
    code = ("import orcad, json; s = orcad.Session(print); "
            "print(json.dumps([orcad.scad.__file__, orcad.page_html()[:15], sorted(orcad.scad.CATALOG['objects'])]))")
    out = subprocess.run([sys.executable, "-c", code], cwd=tmp_path, capture_output=True, text=True, check=True).stdout
    assert str(tmp_path / ".backend") in out and "<!doctype html>" in out and "gridfinity_bin" in out
