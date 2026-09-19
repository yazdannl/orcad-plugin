"""Unit tests for orcad v0.4 — run WITHOUT OrcaSlicer or build123d installed.

Covers pure logic in orcad.py: param validation (number/int/bool),
codegen incl. Gridfinity, live-preview + plate routing, filename hygiene,
examples syntax, preview helper, PAGE_HTML bridge contract.
"""
import ast
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "orcad.py"

ALLOWED_CDN_HOSTS = ("cdn.jsdelivr.net",)  # monaco loader only; CSS is inline


def load_plugin():
    # orca is absent here -> plugin sets orca=None and skips capability classes.
    spec = importlib.util.spec_from_file_location("orcad", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orcad"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = load_plugin()


def test_metadata_block():
    text = PLUGIN.read_text(encoding="utf-8")
    assert "# /// script" in text
    assert 'dependencies = ["build123d", "numpy"]' in text
    assert 'name = "orcad"' in text
    assert 'version = "0.5.1"' in text


def test_primitives_codegen_ok():
    assert "Box(20" in mod.generate_primitive_code("box", {"L": 20, "W": 20, "H": 20})
    assert "Cylinder(10" in mod.generate_primitive_code("cylinder", {"R": 10, "H": 20})
    tube = mod.generate_primitive_code("tube", {"R_OUT": 12, "R_IN": 8, "H": 25})
    assert "Cylinder(12" in tube and "Cylinder(8" in tube
    br = mod.generate_primitive_code("bracket", {"L": 60, "W": 30, "T": 5, "D": 5})
    assert "Pos(" in br and "result =" in br


def test_primitives_validation():
    try:
        mod.validate_primitive_params("tube", {"R_OUT": 8, "R_IN": 8, "H": 10})
        raise AssertionError("expected ValueError for R_IN >= R_OUT")
    except ValueError:
        pass
    try:
        mod.validate_primitive_params("box", {"L": 5000, "W": 20, "H": 20})
        raise AssertionError("expected ValueError for out-of-range")
    except ValueError:
        pass
    try:
        mod.validate_primitive_params("nope", {})
        raise AssertionError("expected ValueError for unknown object")
    except ValueError:
        pass
    # int + bool coercion
    c = mod.validate_primitive_params(
        "gridfinity_bin",
        {"GX": 2.0, "GY": "2", "HU": 6, "WALL": 1.2, "DX": 1, "DY": 0,
         "MAGNETS": 1, "LIP": "false", "SCOOP": "no"})
    assert c == {"GX": 2, "GY": 2, "HU": 6, "WALL": 1.2, "DX": 1, "DY": 0,
                 "MAGNETS": True, "LIP": False, "SCOOP": False}
    try:
        mod.validate_primitive_params(
            "gridfinity_bin",
            {"GX": 2.5, "GY": 2, "HU": 6, "WALL": 1.2, "DX": 0, "DY": 0,
             "MAGNETS": True, "LIP": True, "SCOOP": False})
        raise AssertionError("expected ValueError for non-integer grid")
    except ValueError:
        pass


def test_gridfinity_codegen():
    code = mod.generate_primitive_code(
        "gridfinity_bin",
        {"GX": 2, "GY": 3, "HU": 6, "WALL": 1.2, "DX": 1, "DY": 2,
         "MAGNETS": True, "LIP": True, "SCOOP": True})
    for needle in ("RectangleRounded", "extrude", "GX * 42", "result = _outer - _cavity",
                   "Cylinder(3.25", "result =", "Pos(", "loft(", "ruled=True",
                   "Sketch() + _secs", "divider walls", "scoop notch",
                   "_lip_outer - _lip_inner"):
        assert needle in code, f"gridfinity bin code missing {needle!r}"
    ast.parse(code)
    no_lip = mod.generate_primitive_code(
        "gridfinity_bin",
        {"GX": 1, "GY": 1, "HU": 3, "WALL": 1.2, "DX": 0, "DY": 0,
         "MAGNETS": False, "LIP": False, "SCOOP": False})
    assert "Stacking lip" not in no_lip and "_lip" not in no_lip
    assert "magnet" not in no_lip.lower()
    assert "divider" not in no_lip.lower() and "scoop" not in no_lip.lower()
    ast.parse(no_lip)
    plate = mod.generate_primitive_code(
        "gridfinity_baseplate",
        {"GX": 4, "GY": 4, "T": 5, "SOCKETS": True, "MAGNETS": True})
    assert "RectangleRounded(W, D, 2.0)" in plate and "result -=" in plate
    ast.parse(plate)
    try:
        mod.generate_primitive_code(
            "gridfinity_baseplate",
            {"GX": 4, "GY": 4, "T": 4.0, "SOCKETS": True, "MAGNETS": True})
        raise AssertionError("expected ValueError for thin plate + magnets")
    except ValueError:
        pass


def test_examples_parse():
    for key, ex in mod.EXAMPLES.items():
        assert ex["code"], f"example {key} has empty code"
        tree = ast.parse(ex["code"], filename=f"<example {key}>")
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
        assert "result" in names, f"example {key} must assign `result`"
    for prim in mod.PRIMITIVES:
        code = mod.generate_primitive_code(prim, mod._defaults(prim))
        ast.parse(code)  # must be syntactically valid python


def test_preview_helper_fails_soft_without_build123d():
    # No OCP here: tessellate path must return None, never raise.
    class FakeShape:
        def tessellate(self, *a, **k):
            raise ImportError("no OCP in test env")
    assert mod._preview_payload(FakeShape()) is None
    assert mod.PREVIEW_MAX_TRIS == 3000


def test_page_html_contract():
    html = mod.PAGE_HTML
    # bridge + theming (inline, no framework)
    for needle in ("window.orca.postMessage", "window.orca.onMessage", "--orca-bg"):
        assert needle in html, f"PAGE_HTML missing {needle!r}"
    assert "bootstrap" not in html.lower(), "must not depend on Bootstrap CDN"
    assert "monaco-editor" in html  # editor CDN w/ textarea fallback
    assert "monacoFallback" in html and 'id="code"' in html and 'id="editor"' in html
    # left tabs: objects <-> editor switch
    for needle in ("tabbtn-objs", "tabbtn-editor", "pane-objs", "pane-editor",
                   "switchLeft", "runActive"):
        assert needle in html, f"PAGE_HTML missing left-tab {needle!r}"
    # searchable objects dropdown
    for needle in ("objSearch", "objList", "ddFilter", "ddToggle", "ddLabel"):
        assert needle in html, f"PAGE_HTML missing dropdown {needle!r}"
    # every Python object + example key must exist in the page JS (parity)
    for key in mod.PRIMITIVES:
        assert key in html, f"object {key} missing from page"
    for key in mod.EXAMPLES:
        assert key in html, f"example {key} missing from page"
    # always-on preview pane
    for needle in ('id="pv3d"', "pvSet", "pvDraw", "pvToggleWire", "pvToggleSpin"):
        assert needle in html, f"PAGE_HTML missing preview {needle!r}"
    # result/log plumbing kept
    for needle in ('id="result"', 'id="log"', 'id="fmt"', 'id="tol"'):
        assert needle in html
    # live preview wiring (debounced, seq-guarded, export-free)
    for needle in ("schedulePreview", "currentPayload", "PVSEQ", "preview"):
        assert needle in html, f"PAGE_HTML missing live-preview {needle!r}"
    # send-to-plate wiring
    for needle in ('id="plateBtn"', "sendPlate", "plate_result", "Send to plate"):
        assert needle in html, f"PAGE_HTML missing plate {needle!r}"
    # editor mirror wiring (code section follows the selected object)
    for needle in ("refreshEditorCode", "command:'code'", "d.type==='code'"):
        assert needle in html, f"PAGE_HTML missing editor-mirror {needle!r}"
    # only allowlisted CDNs; everything else self-contained
    for url in re.findall(r'https://[^"\'\s<>]+', html):
        host = url.split("/")[2]
        assert host in ALLOWED_CDN_HOSTS, f"unexpected external URL {url!r}"
    assert len(html) < 200_000, f"PAGE_HTML too large: {len(html)}"


def test_filenames():
    assert mod.sanitize_stem("../../etc/passwd") == "etc_passwd"
    f = mod.stamped_filename("my model!", "stl")
    assert f.endswith(".stl") and "my_model" in f


def test_runner_rejects_bad_input_without_build123d():
    try:
        mod.run_build123d_code("result = 1", export_format="obj")
        raise AssertionError("expected ValueError for bad format")
    except ValueError:
        pass
    try:
        mod.run_build123d_code("x" * 300_000)
        raise AssertionError("expected ValueError for oversize code")
    except ValueError:
        pass
    try:
        mod.preview_shape("x" * 300_000)
        raise AssertionError("expected ValueError for oversize preview code")
    except ValueError:
        pass
    try:
        mod.preview_shape("result = 1", tolerance=99)
        raise AssertionError("expected ValueError for bad preview tolerance")
    except ValueError:
        pass


def test_preview_reports_missing_build123d():
    try:
        mod.preview_shape("result = Box(1, 1, 1)")
        raise AssertionError("expected RuntimeError without build123d")
    except RuntimeError as exc:
        assert "build123d is not installed" in str(exc)


def test_code_command_is_sync_codegen():
    class FakeCap:
        def post_message(self, d):
            raise AssertionError("code command must not spawn worker posts")

    cap = FakeCap()
    res = mod._handle_message_sync(cap, {"command": "code", "kind": "generate",
                                         "primitive": "box",
                                         "params": {"L": 5, "W": 6, "H": 7}})
    assert res["type"] == "code" and res["ok"] is True
    assert "Box(5" in res["code"] and "result =" in res["code"]
    # gridfinity code mirrors the objects tab state
    res = mod._handle_message_sync(cap, {"command": "code", "kind": "generate",
                                         "primitive": "gridfinity_bin",
                                         "params": mod._defaults("gridfinity_bin")})
    assert res["ok"] is True and "loft(" in res["code"]
    # invalid params -> immediate error, editor keeps last good code
    res = mod._handle_message_sync(cap, {"command": "code", "kind": "generate",
                                         "primitive": "nope", "params": {}})
    assert res["type"] == "code" and res["ok"] is False


def test_preview_message_routing():
    import time

    class FakeCap:
        def __init__(self):
            self.posts = []

        def post_message(self, d):
            self.posts.append(d)

    # sync validation error -> immediate preview error, no thread
    cap = FakeCap()
    res = mod._handle_message_sync(cap, {"command": "preview", "kind": "generate",
                                         "primitive": "nope", "params": {}})
    assert res["type"] == "preview" and res["ok"] is False
    assert cap.posts == []
    # async path -> worker posts preview error (no build123d here)
    cap = FakeCap()
    assert mod._handle_message_sync(cap, {"command": "preview", "kind": "generate",
                                          "primitive": "box",
                                          "params": {"L": 1, "W": 1, "H": 1},
                                          "seq": 7}) is None
    deadline = time.time() + 5
    while time.time() < deadline and not cap.posts:
        time.sleep(0.05)
    assert cap.posts and cap.posts[-1]["type"] == "preview"
    assert cap.posts[-1].get("seq") == 7
    # stale seq -> worker stays silent (preview gated until seq moves on)
    import threading as _th
    from unittest import mock as _mock
    gate = _th.Event()

    def slow_preview(code, tolerance=0.001):
        assert gate.wait(5)
        return {"ok": True, "var": "result", "stats": {}, "preview": None}

    cap = FakeCap()
    with _mock.patch.object(mod, "preview_shape", side_effect=slow_preview):
        mod._handle_message_sync(cap, {"command": "preview", "kind": "generate",
                                       "primitive": "box",
                                       "params": {"L": 1, "W": 1, "H": 1}, "seq": 1})
        mod._handle_message_sync(cap, {"command": "preview", "kind": "generate",
                                       "primitive": "box",
                                       "params": {"L": 2, "W": 2, "H": 2}, "seq": 2})
        gate.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(p.get("type") == "preview" for p in cap.posts):
                break
            time.sleep(0.05)
    seqs = [p.get("seq") for p in cap.posts if p.get("type") == "preview"]
    assert seqs == [2], seqs


def test_plate_message_routing():
    import time

    class FakeCap:
        def __init__(self):
            self.posts = []

        def post_message(self, d):
            self.posts.append(d)

    cap = FakeCap()
    ack = mod._handle_message_sync(cap, {"command": "plate", "kind": "generate",
                                         "primitive": "box",
                                         "params": {"L": 1, "W": 1, "H": 1}})
    assert ack["type"] == "progress"
    deadline = time.time() + 6
    while time.time() < deadline:
        if any(p.get("type") == "plate_result" for p in cap.posts):
            break
        time.sleep(0.05)
    finals = [p for p in cap.posts if p.get("type") == "plate_result"]
    assert finals and finals[-1]["ok"] is False  # no build123d in test env


def test_open_with_default_app_failure():
    from unittest import mock
    with mock.patch.object(mod.subprocess, "Popen", side_effect=OSError("no opener")):
        try:
            mod._open_with_default_app("/tmp/nonexistent_dir/x.stl")
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "Could not hand" in str(exc)

    from unittest.mock import MagicMock
    with mock.patch.object(mod.subprocess, "Popen", return_value=MagicMock()):
        mod._open_with_default_app("/tmp/x.stl")  # must not raise


def _load_source_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_objects_live_in_their_own_files():
    expected = ["box", "bracket", "cylinder", "gridfinity_baseplate",
                "gridfinity_bin", "tube"]
    assert sorted(p.stem for p in (ROOT / "objects").glob("*.py")
                  if p.name != "__init__.py") == expected
    for name in expected:
        obj = _load_source_module(f"objects_{name}", ROOT / "objects" / f"{name}.py")
        assert isinstance(obj.SPEC.get("params"), list)
        defaults = {p["key"]: p["default"] for p in obj.SPEC["params"]}
        ast.parse(obj.generate(defaults))
    registry = None
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import objects as registry
    assert sorted(registry.OBJECTS) == expected


def test_bundle_in_sync():
    bundle = _load_source_module("bundle", ROOT / "packaging" / "bundle.py")
    assert bundle.check(), "orcad.py out of sync — run python3 packaging/bundle.py --write"
