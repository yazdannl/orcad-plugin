"""Unit tests for OrcaCAD v0.2 — run WITHOUT OrcaSlicer or build123d installed.

Covers pure logic in orcacad_plugin.py: param validation, codegen,
filename hygiene, examples syntax, preview helper, PAGE_HTML bridge contract.
"""
import ast
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "orcacad_plugin.py"

ALLOWED_CDN_HOSTS = ("cdn.jsdelivr.net", "unpkg.com")


def load_plugin():
    # orca is absent here -> plugin sets orca=None and skips capability classes.
    spec = importlib.util.spec_from_file_location("orcacad_plugin", PLUGIN)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["orcacad_plugin"] = mod
    spec.loader.exec_module(mod)
    return mod


mod = load_plugin()


def test_metadata_block():
    text = PLUGIN.read_text(encoding="utf-8")
    assert "# /// script" in text
    assert 'dependencies = ["build123d", "numpy"]' in text
    assert 'name = "OrcaCAD"' in text
    assert 'version = "0.2.0"' in text


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
        raise AssertionError("expected ValueError for unknown primitive")
    except ValueError:
        pass


def test_examples_parse():
    for key, ex in mod.EXAMPLES.items():
        tree = ast.parse(ex["code"], filename=f"<example {key}>")
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Name) and isinstance(node.ctx, ast.Store):
                names.add(node.id)
        assert "result" in names, f"example {key} must assign `result`"
    for prim in mod.PRIMITIVES:
        defaults = {p["key"]: p["default"] for p in mod.PRIMITIVES[prim]["params"]}
        code = mod.generate_primitive_code(prim, defaults)
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
    # bridge + theming
    for needle in ("window.orca.postMessage", "window.orca.onMessage", "--orca-bg"):
        assert needle in html, f"PAGE_HTML missing {needle!r}"
    # Bootstrap 5 + Monaco present
    assert "bootstrap@5" in html
    assert "monaco-editor" in html
    # left tabs: primitives <-> editor switch
    for needle in ("tabbtn-prims", "tabbtn-editor", "pane-prims", "pane-editor",
                   "switchLeft", "runActive"):
        assert needle in html, f"PAGE_HTML missing left-tab {needle!r}"
    # editor ids (monaco container + textarea fallback share the code flow)
    assert 'id="editor"' in html and 'id="code"' in html
    assert "monacoFallback" in html
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
