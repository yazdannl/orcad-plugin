"""Unit tests for orcad v0.3 — run WITHOUT OrcaSlicer or build123d installed.

Covers pure logic in orcad.py: param validation (number/int/bool),
codegen incl. Gridfinity, filename hygiene, examples syntax, preview helper,
PAGE_HTML bridge contract (self-contained CSS, Monaco only CDN).
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
    assert 'version = "0.3.0"' in text


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
        {"GX": 2.0, "GY": "2", "HU": 6, "WALL": 1.2, "MAGNETS": 1, "LIP": "false"})
    assert c == {"GX": 2, "GY": 2, "HU": 6, "WALL": 1.2, "MAGNETS": True, "LIP": False}
    try:
        mod.validate_primitive_params(
            "gridfinity_bin",
            {"GX": 2.5, "GY": 2, "HU": 6, "WALL": 1.2, "MAGNETS": True, "LIP": True})
        raise AssertionError("expected ValueError for non-integer grid")
    except ValueError:
        pass


def test_gridfinity_codegen():
    code = mod.generate_primitive_code(
        "gridfinity_bin",
        {"GX": 2, "GY": 3, "HU": 6, "WALL": 1.2, "MAGNETS": True, "LIP": True})
    for needle in ("RectangleRounded", "extrude", "GX * 42", "result = _outer - _cavity",
                   "Cylinder(3.25", "result =", "Pos("):
        assert needle in code, f"gridfinity bin code missing {needle!r}"
    ast.parse(code)
    no_lip = mod.generate_primitive_code(
        "gridfinity_bin",
        {"GX": 1, "GY": 1, "HU": 3, "WALL": 1.2, "MAGNETS": False, "LIP": False})
    assert "Stacking lip" not in no_lip and "_lip" not in no_lip
    assert "magnet" not in no_lip.lower()
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
