"""Unit tests for orcad — run WITHOUT OrcaSlicer or build123d installed.

Covers pure logic in orcad.py: param validation (number/int/bool), object
programs (spec extraction, value baking), live-preview + plate routing,
filename hygiene, examples syntax, preview helper, PAGE_HTML bridge contract.
"""
import ast
import importlib.util
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN = ROOT / "orcad.py"

def load_plugin():
    # orca is absent here -> plugin sets orca=None and skips capability classes.
    spec = importlib.util.spec_from_file_location("orcad", PLUGIN)
    assert spec is not None and spec.loader is not None
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
    assert 'version = "0.6.0"' in text


def test_ui_metadata_is_bundled():
    params = {param["key"]: param for param in mod.PRIMITIVES["gridfinity_bin"]["params"]}
    assert params["CD"]["ui"] == {
        "group": "Compartments",
        "dependsOn": "CYL",
        "help": "Used only when cylindrical compartments is enabled.",
    }
    assert params["REFINED"]["ui"]["exclusiveWith"] == "MAGNETS"
    assert "ui" not in mod.PRIMITIVES["box"]["params"][0]


def test_primitives_codegen_ok():
    box = mod.generate_primitive_code("box", {"L": 20, "W": 20, "H": 20})
    assert "L = 20.0" in box and "result = Box(L, W, H)" in box
    cyl = mod.generate_primitive_code("cylinder", {"R": 10, "H": 20})
    assert "R = 10.0" in cyl and "result = Cylinder(R, H)" in cyl
    tube = mod.generate_primitive_code("tube", {"R_OUT": 12, "R_IN": 8, "H": 25})
    assert "R_OUT = 12.0" in tube and "Cylinder(R_OUT, H)" in tube
    br = mod.generate_primitive_code("bracket", {"L": 60, "W": 30, "T": 5, "D": 5})
    assert "L = 60.0" in br and "result = plate - h1 - h2" in br


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
        {"GX": 2.0, "GY": "2", "HU": 6, "HMODE": 0, "ZS": 0, "FILL": 0,
         "WALL": 1.2, "DX": 1, "DY": 0, "DEPTH": 0, "SCOOPW": "1",
         "TABSTYLE": 1, "TABPLACE": 0, "CYL": "false", "CD": 10, "CCHAM": 0.5,
         "REFINED": 0, "MAGNETS": 1, "SCREW": 0, "CRUSH": 1, "CHAMFER": 1,
         "PRINTABLE": 0, "CORNERS": 0, "THUMB": 0, "LIP": "false"})
    assert c["GX"] == 2 and c["GY"] == 2 and c["HU"] == 6
    assert c["MAGNETS"] is True and c["LIP"] is False and c["CYL"] is False
    assert c["SCOOPW"] == 1.0 and c["TABSTYLE"] == 1 and c["HMODE"] == 0
    try:
        mod.validate_primitive_params(
            "gridfinity_bin",
            dict(c, GX=2.5))
        raise AssertionError("expected ValueError for non-integer grid")
    except ValueError:
        pass
    try:
        mod.validate_primitive_params(
            "gridfinity_bin",
            dict(c, REFINED=True, MAGNETS=True))
        raise AssertionError("expected ValueError for refined+magnet")
    except ValueError:
        pass


def _gbin(**over):
    params = {"GX": 2, "GY": 3, "HU": 6, "HMODE": 0, "ZS": False, "FILL": 0,
              "WALL": 1.2, "DX": 1, "DY": 2, "DEPTH": 0, "SCOOPW": 1.0,
              "TABSTYLE": 1, "TABPLACE": 0, "CYL": False, "CD": 10, "CCHAM": 0.5,
              "REFINED": False, "MAGNETS": True, "SCREW": False, "CRUSH": False,
              "CHAMFER": False, "PRINTABLE": False, "CORNERS": False,
              "THUMB": False, "LIP": True}
    params.update(over)
    return mod.generate_primitive_code("gridfinity_bin", params)


def _expect_parameter_error(call, fields, phrase):
    try:
        call()
    except mod.ParameterValidationError as exc:
        assert {error["field"] for error in exc.errors} >= set(fields)
        assert phrase in str(exc)
        return exc
    raise AssertionError("expected parameter validation error")


def test_cross_field_geometry_boundaries_and_errors():
    # Equality leaves the documented divider/base clearances intact.
    _gbin(FILL=33.8, DEPTH=33.8, CYL=True, CD=39, CCHAM=0.5)
    plate = {key: value for key, value in mod._defaults("gridfinity_baseplate").items()}
    plate.update(SCREW=True, CHAMFER=True, SCREW_D=5, SCREW_HEAD=5)
    mod.validate_primitive_params("gridfinity_baseplate", plate)

    _expect_parameter_error(
        lambda: _gbin(FILL=10, DEPTH=10.1), ("DEPTH", "FILL"),
        "does not remove the base wall")
    _expect_parameter_error(
        lambda: _gbin(GX=1, GY=1, FILL=10, CYL=True, CD=39.5, CCHAM=0.5), ("CD", "CCHAM"),
        "cylindrical opening")
    _expect_parameter_error(
        lambda: _gbin(HU=1, FILL=1, LIP=True), ("FILL", "HU", "LIP"),
        "increase height")
    plate.update(SCREW_D=5, SCREW_HEAD=4.9)
    _expect_parameter_error(
        lambda: mod.validate_primitive_params("gridfinity_baseplate", plate),
        ("SCREW_HEAD", "SCREW_D"), "at least the screw diameter")


def test_invalid_object_messages_reject_before_cad_for_all_object_commands():
    from unittest import mock

    class FakeCap:
        def post_message(self, message):
            raise AssertionError(f"CAD work must not post: {message}")

    invalid = {"HU": 1, "FILL": 1, "LIP": True}
    for command in ("generate", "preview", "plate"):
        message = {"command": command, "kind": "generate", "primitive": "gridfinity_bin",
                   "params": dict(mod._defaults("gridfinity_bin"), **invalid),
                   "request_id": 101, "revision_id": 7, "seq": 3}
        with (mock.patch.object(mod, "run_build123d_code") as build,
              mock.patch.object(mod, "preview_shape") as preview):
            result = mod._handle_message_sync(FakeCap(), message)
        assert result is not None and result["ok"] is False
        assert result["errors"]
        assert {error["field"] for error in result["errors"]} >= {"FILL", "HU", "LIP"}
        build.assert_not_called()
        preview.assert_not_called()


def test_gridfinity_codegen():
    code = _gbin()
    for needle in ("RectangleRounded", "extrude", "GX * 42", "result =", "Pos(",
                   "loft(", "ruled=True", "Sketch() + _secs", "make_face",
                   "Polyline", "import math", "GX = 2", "WALL = 1.2",
                   "DX = 1", "DY = 2", "SCOOPW = 1.0"):
        assert needle in code, f"gridfinity bin code missing {needle!r}"
    ast.parse(code)
    bare = _gbin(DX=0, DY=0, MAGNETS=False, LIP=False, SCOOPW=0, TABSTYLE=5)
    assert "LIP = False" in bare and "DX = 0" in bare
    ast.parse(bare)
    plate = mod.generate_primitive_code(
        "gridfinity_baseplate",
        {"GX": 4, "GY": 4, "T": 5, "STYLE": 0, "HOLESTYLE": 0,
         "DISTX": 0, "DISTY": 0, "FITX": 0, "FITY": 0, "SCREW_D": 3.35,
         "SCREW_HEAD": 5, "SCREW_SPACING": 0.5, "NSCREWS": 1,
         "SOCKETS": True, "REFINED": False, "MAGNETS": True, "SCREW": False,
         "CRUSH": True, "CHAMFER": True, "PRINTABLE": False, "CORNERS": False})
    assert "RectangleRounded(W, D, 2.0)" in plate and "result -=" in plate
    ast.parse(plate)
    try:
        mod.generate_primitive_code(
            "gridfinity_baseplate",
            {"GX": 4, "GY": 4, "T": 4.0, "STYLE": 0, "HOLESTYLE": 0,
             "DISTX": 0, "DISTY": 0, "FITX": 0, "FITY": 0, "SCREW_D": 3.35,
             "SCREW_HEAD": 5, "SCREW_SPACING": 0.5, "NSCREWS": 1,
             "SOCKETS": True, "REFINED": False, "MAGNETS": True, "SCREW": False,
             "CRUSH": True, "CHAMFER": True, "PRINTABLE": False, "CORNERS": False})
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


def test_shape_stats_expose_named_dimensions_and_units_without_changing_legacy_fields():
    from types import SimpleNamespace

    shape = SimpleNamespace(
        volume=123.4567,
        area=lambda: 89.1234,
        bounding_box=lambda: SimpleNamespace(
            min=(0, -2, 1), max=(20, 28, 41), size=(20, 30, 40)))
    stats = mod._shape_stats(shape)
    assert stats["volume_mm"] == 123.457 and stats["area_mm"] == 89.123
    assert stats["volume_mm3"] == 123.457 and stats["area_mm2"] == 89.123
    assert stats["dimensions_mm"] == {"width": 20, "depth": 30, "height": 40}
    assert stats["width_mm"] == 20 and stats["depth_mm"] == 30 and stats["height_mm"] == 40
    assert stats["bbox_size"] == [20, 30, 40]


def test_preview_payload_keeps_complete_tessellation():
    from types import SimpleNamespace

    vertices = [SimpleNamespace(X=0, Y=0, Z=0),
                SimpleNamespace(X=1, Y=0, Z=0),
                SimpleNamespace(X=0, Y=1, Z=0)]
    triangles = [(0, 1, 2)] * 6001

    class FakeShape:
        def tessellate(self, *args):
            return vertices, triangles

    payload = mod._preview_payload(FakeShape())
    assert payload["total"] == payload["shown"] == len(triangles)
    assert len(payload["tris"]) == len(triangles) * 9


def test_page_html_contract():
    html = mod.PAGE_HTML
    assert mod.PAGE_ASSET.is_file()
    for needle in ("window.orca.postMessage", "window.orca?.onMessage", "--orca-bg"):
        assert needle in html, f"PAGE_HTML missing {needle!r}"
    assert "bootstrap" not in html.lower(), "must not depend on Bootstrap CDN"
    assert '<div id="app"></div>' in html
    assert "Vue" in html and "Three.js" in html and ".min-h-screen" in html
    for needle in ("Objects", "Code", "objectSearch", "plate_result", "Run / export",
                   "Wireframe", "Spin", "Send to plate", "Copy path",
                   "Open exports folder", "drag the file", "drag to rotate"):
        assert needle in html, f"PAGE_HTML missing frontend feature {needle!r}"
    for key in mod.PRIMITIVES:
        assert key in html, f"object {key} missing from frontend"
    for key in mod.EXAMPLES:
        assert key in html, f"example {key} missing from frontend"
    assert "vite" not in html.lower(), "build tooling must not ship in the page"
    assert not re.search(r'<(?:script|link)[^>]+https?://', html), "frontend must not load network assets"
    assert len(html) < 700_000, f"compiled frontend too large: {len(html)}"


def test_compiled_frontend_fallback():
    original = mod.__dict__["PAGE_ASSET"]
    try:
        mod.__dict__["PAGE_ASSET"] = ROOT / "missing-frontend" / "index.html"
        assert mod._load_page_html() == mod.PAGE_HTML
    finally:
        mod.__dict__["PAGE_ASSET"] = original


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
        message = str(exc)
        assert "build123d/OCP is not ready" in message
        assert "hundreds of MB" in message
        assert "network and write access" in message
        assert "restart OrcaSlicer" in message
        assert "retry dependency setup" in message


def test_setup_trust_and_result_guidance_stays_consistent():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    html = mod.PAGE_HTML
    for source in (readme, html):
        assert "hundreds of MB" in source
        assert "network and write access" in source
        assert "no security sandbox" in source.lower()
        assert "validation/UX only" in source
        assert "result" in source
    assert "End users need no Node.js or CDN to load the" in readme
    assert "End users need no Node.js, CDN, or network" not in readme
    assert "Bridge" in html and "CAD/model" in html
    assert "CAD/model not checked" in html
    assert "there is no separate dependency probe" in html


def test_exports_route_formats_and_tessellation_settings(tmp_path):
    from types import SimpleNamespace
    from unittest import mock

    calls = {}

    def write_file(path):
        Path(path).write_bytes(b"exported")
        return True

    def export_stl(shape, path, **kwargs):
        calls["stl"] = (shape, kwargs)
        return write_file(path)

    def export_step(shape, path):
        calls["step"] = (shape, {})
        return write_file(path)

    class FakeMesher:
        def __init__(self):
            self.added = None

        def add_shape(self, shape, **kwargs):
            self.added = (shape, kwargs)
            calls["3mf"] = self.added

        def write(self, path):
            write_file(path)

    fake_build123d = SimpleNamespace(
        export_stl=export_stl, export_step=export_step, Mesher=FakeMesher,
    )
    with mock.patch.object(mod, "_execute_code", return_value=("shape", "result")), \
         mock.patch.object(mod, "_shape_stats", return_value={}), \
         mock.patch.object(mod, "_preview_payload", return_value=None), \
         mock.patch.object(mod, "exports_dir", return_value=tmp_path), \
         mock.patch.dict(sys.modules, {"build123d": fake_build123d}):
        results = [mod.run_build123d_code("ignored", fmt, 0.02, fmt)
                   for fmt in ("stl", "step", "3mf")]

    assert [result["format"] for result in results] == ["stl", "step", "3mf"]
    assert all(Path(result["file"]).read_bytes() == b"exported" for result in results)
    assert calls["stl"][1] == {"tolerance": 0.02, "angular_tolerance": 0.1}
    assert calls["step"][1] == {}
    assert calls["3mf"][1] == {"linear_deflection": 0.02, "angular_deflection": 0.1}


def test_exports_are_unique_within_one_timestamp(tmp_path):
    from types import SimpleNamespace
    from unittest import mock

    fixed = mod.datetime.datetime(2026, 1, 2, 3, 4, 5)

    class FixedDateTime:
        @classmethod
        def now(cls):
            return fixed

    def export_stl(shape, path, **kwargs):
        Path(path).write_bytes(b"stl")
        return True

    fake_build123d = SimpleNamespace(export_stl=export_stl)
    with (
        mock.patch.object(mod, "_execute_code", return_value=("shape", "result")),
        mock.patch.object(mod, "_shape_stats", return_value={}),
        mock.patch.object(mod, "_preview_payload", return_value=None),
        mock.patch.object(mod, "exports_dir", return_value=tmp_path),
        mock.patch.dict(sys.modules, {"build123d": fake_build123d}),
    ):
        results = [mod.run_build123d_code("ignored", filename_stem="box") for _ in range(3)]

    assert len({result["filename"] for result in results}) == 3
    assert all(Path(result["file"]).read_bytes() == b"stl" for result in results)
    assert sorted(path.name for path in tmp_path.iterdir()) == sorted(
        result["filename"] for result in results)


def test_concurrent_exports_do_not_overwrite(tmp_path):
    from concurrent.futures import ThreadPoolExecutor
    from types import SimpleNamespace
    from unittest import mock

    barrier = __import__("threading").Barrier(6)

    def export_stl(shape, path, **kwargs):
        barrier.wait(5)
        Path(path).write_bytes(b"stl")
        return True

    fake_build123d = SimpleNamespace(export_stl=export_stl)
    with (
        mock.patch.object(mod, "_execute_code", return_value=("shape", "result")),
        mock.patch.object(mod, "_shape_stats", return_value={}),
        mock.patch.object(mod, "_preview_payload", return_value=None),
        mock.patch.object(mod, "exports_dir", return_value=tmp_path),
        mock.patch.dict(sys.modules, {"build123d": fake_build123d}),
        ThreadPoolExecutor(max_workers=6) as pool,
    ):
        results = list(pool.map(
            lambda _: mod.run_build123d_code("ignored", filename_stem="same"),
            range(6)))

    assert len({result["filename"] for result in results}) == 6
    assert len(list(tmp_path.iterdir())) == 6
    assert all(result["size_bytes"] == 3 for result in results)


def test_failed_exporters_clean_up_partial_files(tmp_path):
    from types import SimpleNamespace
    from unittest import mock

    def export_stl(shape, path, **kwargs):
        Path(path).write_bytes(b"partial")
        raise OSError("writer failed")

    def export_step(shape, path):
        Path(path).write_bytes(b"partial")
        return False

    fake_build123d = SimpleNamespace(export_stl=export_stl, export_step=export_step)
    with (
        mock.patch.object(mod, "_execute_code", return_value=("shape", "result")),
        mock.patch.object(mod, "exports_dir", return_value=tmp_path),
        mock.patch.dict(sys.modules, {"build123d": fake_build123d}),
    ):
        for export_format in ("stl", "step"):
            try:
                mod.run_build123d_code("ignored", export_format, filename_stem="failed")
                raise AssertionError("expected RuntimeError")
            except RuntimeError:
                pass
            assert not list(tmp_path.iterdir())


def test_missing_or_empty_export_is_failure_and_cleans_up(tmp_path):
    from types import SimpleNamespace
    from unittest import mock

    for payload in (None, b""):
        def export_stl(shape, path, payload=payload, **kwargs):
            if payload is not None:
                Path(path).write_bytes(payload)
            return True

        fake_build123d = SimpleNamespace(export_stl=export_stl)
        with (
            mock.patch.object(mod, "_execute_code", return_value=("shape", "result")),
            mock.patch.object(mod, "exports_dir", return_value=tmp_path),
            mock.patch.dict(sys.modules, {"build123d": fake_build123d}),
        ):
            try:
                mod.run_build123d_code("ignored", filename_stem="missing")
                raise AssertionError("expected RuntimeError")
            except RuntimeError:
                pass
        assert not list(tmp_path.iterdir())


def test_code_command_is_sync_codegen():
    class FakeCap:
        def post_message(self, d):
            raise AssertionError("code command must not spawn worker posts")

    cap = FakeCap()
    res = mod._handle_message_sync(cap, {"command": "code", "kind": "generate",
                                         "primitive": "box",
                                         "params": {"L": 5, "W": 6, "H": 7},
                                         "request_id": 17})
    assert res["type"] == "code" and res["ok"] is True
    assert res["request_id"] == 17
    assert "L = 5.0" in res["code"] and "result = Box(L, W, H)" in res["code"]
    # gridfinity code mirrors the objects tab state
    res = mod._handle_message_sync(cap, {"command": "code", "kind": "generate",
                                         "primitive": "gridfinity_bin",
                                         "params": mod._defaults("gridfinity_bin")})
    assert res["ok"] is True and "loft(" in res["code"]
    # invalid params -> immediate error, editor keeps last good code
    res = mod._handle_message_sync(cap, {"command": "code", "kind": "generate",
                                         "primitive": "nope", "params": {},
                                         "request_id": 18, "revision_id": 3})
    assert res["type"] == "code" and res["ok"] is False
    assert res["request_id"] == 18 and res["revision_id"] == 3


def test_cad_scheduler_coalesces_prioritizes_exports_and_cancels_exact_pending_job():
    import threading

    scheduler = mod._CadJobScheduler()
    events = []
    first_started = threading.Event()
    release_first = threading.Event()
    finished = threading.Event()

    def first_preview():
        events.append("preview-1")
        first_started.set()
        assert release_first.wait(2)

    def last_preview():
        events.append("preview-3")
        finished.set()

    assert scheduler.submit_preview(first_preview) is None
    assert first_started.wait(2)
    scheduler.submit_preview(lambda: events.append("preview-2"))
    scheduler.submit_preview(last_preview)
    assert scheduler.submit_export(("export", "box"), lambda: events.append("export"))
    assert not scheduler.submit_export(("export", "box"), lambda: events.append("duplicate"))

    release_first.set()
    assert finished.wait(2)
    assert events == ["preview-1", "export", "preview-3"]

    scheduler = mod._CadJobScheduler()
    started = threading.Event()
    release = threading.Event()
    finished = threading.Event()
    events = []

    def active_export():
        started.set()
        assert release.wait(2)
        events.append("active")
        finished.set()

    assert scheduler.submit_export(("same",), active_export, ("export", 1, 1))
    assert started.wait(2)
    assert scheduler.submit_export(("later",), lambda: events.append("later"), ("export", 2, 1))
    assert scheduler.cancel(("export", 2, 1)) == "pending"
    assert scheduler.cancel(("export", 1, 1)) == "running"
    release.set()
    assert finished.wait(2)
    assert scheduler.cancel(("export", 2, 1)) is None
    assert events == ["active"]


def test_runner_reports_build_tessellation_and_export_stages(tmp_path):
    from types import SimpleNamespace
    from unittest import mock

    stages = []

    def export_stl(shape, path, **kwargs):
        Path(path).write_bytes(b"stl")
        return True

    fake_build123d = SimpleNamespace(export_stl=export_stl)
    with (
        mock.patch.object(mod, "_execute_code", return_value=("shape", "result")),
        mock.patch.object(mod, "_shape_stats", return_value={}),
        mock.patch.object(mod, "_preview_payload", return_value=None),
        mock.patch.object(mod, "exports_dir", return_value=tmp_path),
        mock.patch.dict(sys.modules, {"build123d": fake_build123d}),
    ):
        result = mod.run_build123d_code(
            "ignored", progress=lambda stage, message: stages.append((stage, message)))

    assert result["ok"] is True
    assert [stage for stage, _message in stages] == [
        "building", "tessellating", "exporting", "tessellating"]
    assert not any("%" in message for _stage, message in stages)


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
                                         "primitive": "nope", "params": {},
                                         "request_id": 20, "revision_id": 4, "seq": 9})
    assert res["type"] == "preview" and res["ok"] is False
    assert res["request_id"] == 20 and res["revision_id"] == 4 and res["seq"] == 9
    assert cap.posts == []
    # async path -> worker posts preview error (no build123d here)
    cap = FakeCap()
    assert mod._handle_message_sync(cap, {"command": "preview", "kind": "generate",
                                          "primitive": "box",
                                          "params": {"L": 1, "W": 1, "H": 1},
                                          "request_id": 21, "revision_id": 5, "seq": 7}) is None
    deadline = time.time() + 5
    while time.time() < deadline and not cap.posts:
        time.sleep(0.05)
    assert cap.posts and cap.posts[-1]["type"] == "preview"
    assert cap.posts[-1].get("seq") == 7
    assert cap.posts[-1]["request_id"] == 21 and cap.posts[-1]["revision_id"] == 5
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
                                       "params": {"L": 1, "W": 1, "H": 1},
                                       "request_id": 30, "revision_id": 6, "seq": 1})
        mod._handle_message_sync(cap, {"command": "preview", "kind": "generate",
                                       "primitive": "box",
                                       "params": {"L": 2, "W": 2, "H": 2},
                                       "request_id": 31, "revision_id": 7, "seq": 2})
        gate.set()
        deadline = time.time() + 5
        while time.time() < deadline:
            if any(p.get("type") == "preview" for p in cap.posts):
                break
            time.sleep(0.05)
    seqs = [p.get("seq") for p in cap.posts if p.get("type") == "preview"]
    assert seqs == [2], seqs


def test_canceled_running_preview_discards_its_response():
    import threading
    import time
    from unittest import mock

    class FakeCap:
        def __init__(self):
            self.posts = []

        def post_message(self, d):
            self.posts.append(d)

    started = threading.Event()
    release = threading.Event()

    def slow_preview(code, tolerance=0.001):
        started.set()
        assert release.wait(5)
        return {"ok": True, "var": "result", "stats": {}, "preview": None}

    cap = FakeCap()
    message = {"command": "preview", "kind": "generate", "primitive": "box",
               "params": {"L": 1, "W": 1, "H": 1}, "request_id": 35,
               "revision_id": 7, "seq": 4}
    with mock.patch.object(mod, "preview_shape", side_effect=slow_preview):
        mod._handle_message_sync(cap, message)
        assert started.wait(2)
        canceled = mod._handle_message_sync(cap, {
            "command": "cancel", "target_type": "preview",
            "target_request_id": 35, "target_revision_id": 7, "target_seq": 4,
            "request_id": 36, "revision_id": 7})
        assert canceled["type"] == "cancelled"
        assert canceled["pending"] is False
        release.set()
        time.sleep(0.1)

    assert not [post for post in cap.posts if post.get("type") == "preview"]


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
                                         "params": {"L": 1, "W": 1, "H": 1},
                                         "request_id": 40, "revision_id": 8})
    assert ack["type"] == "progress"
    assert ack["request_id"] == 40 and ack["revision_id"] == 8
    deadline = time.time() + 6
    while time.time() < deadline:
        if any(p.get("type") == "plate_result" for p in cap.posts):
            break
        time.sleep(0.05)
    finals = [p for p in cap.posts if p.get("type") == "plate_result"]
    assert finals and finals[-1]["ok"] is False  # no build123d in test env
    assert finals[-1]["request_id"] == 40 and finals[-1]["revision_id"] == 8


def test_open_with_default_app_selects_linux_macos_windows_and_reports_denials(tmp_path):
    from unittest import mock
    from unittest.mock import MagicMock

    path = tmp_path / "model.stl"
    with mock.patch.object(mod.sys, "platform", "linux"), \
         mock.patch.object(mod.subprocess, "Popen", return_value=MagicMock()) as popen:
        assert mod._open_with_default_app(path) is True
        assert popen.call_args.args[0] == ["xdg-open", str(path)]

    with mock.patch.object(mod.sys, "platform", "linux"), \
         mock.patch.object(mod.subprocess, "Popen", side_effect=[FileNotFoundError(), MagicMock()]) as popen:
        assert mod._open_with_default_app(path) is True
        assert popen.call_args.args[0] == ["gio", "open", str(path)]

    with mock.patch.object(mod.sys, "platform", "darwin"), \
         mock.patch.object(mod.subprocess, "Popen", return_value=MagicMock()) as popen:
        mod._open_with_default_app(path)
        assert popen.call_args.args[0] == ["open", str(path)]

    with mock.patch.object(mod.sys, "platform", "win32"), \
         mock.patch.object(mod.os, "startfile", create=True) as startfile:
        mod._open_with_default_app(path)
        startfile.assert_called_once_with(str(path))

    with mock.patch.object(mod.sys, "platform", "darwin"), \
         mock.patch.object(mod.subprocess, "Popen", side_effect=PermissionError("denied")):
        try:
            mod._open_with_default_app(path)
            raise AssertionError("expected RuntimeError")
        except RuntimeError as exc:
            assert "Could not hand" in str(exc) and "denied" in str(exc)


def test_plate_result_separates_export_and_open_request_and_retains_path(tmp_path):
    import time
    from unittest import mock

    class FakeCap:
        def __init__(self):
            self.posts = []

        def post_message(self, message):
            self.posts.append(message)

    def wait_for_result(cap):
        deadline = time.time() + 5
        while time.time() < deadline:
            results = [post for post in cap.posts if post.get("type") == "plate_result"]
            if results:
                return results[-1]
            time.sleep(0.02)
        raise AssertionError(f"no plate result: {cap.posts}")

    path = tmp_path / "box.stl"
    path.write_bytes(b"valid stl")
    exported = {"file": str(path), "filename": path.name, "format": "stl",
                "size_bytes": path.stat().st_size, "stats": {}, "preview": None,
                "var": "result"}
    message = {"command": "plate", "kind": "code", "code": "result = 1",
               "filename": "box", "tolerance": 0.01, "request_id": 51,
               "revision_id": 9}

    cap = FakeCap()
    with mock.patch.object(mod, "run_build123d_code", return_value=exported), \
         mock.patch.object(mod, "_open_with_default_app", side_effect=PermissionError("denied")):
        mod._handle_message_sync(cap, message)
        failed = wait_for_result(cap)
    assert failed["ok"] is False
    assert failed["export_ok"] is True
    assert failed["open_request_sent"] is False
    assert failed["handoff_status"] == "open_request_failed"
    assert failed["file"] == str(path)
    assert "denied" in failed["open_request_error"]

    cap = FakeCap()
    with mock.patch.object(mod, "run_build123d_code", return_value=exported), \
         mock.patch.object(mod, "_open_with_default_app", return_value=True):
        mod._handle_message_sync(cap, {**message, "request_id": 52})
        sent = wait_for_result(cap)
    assert sent["ok"] is True and sent["export_ok"] is True
    assert sent["open_request_sent"] is True
    assert sent["message"] == "open request sent"
    assert "import" not in sent["message"].lower()


def test_plate_reuses_only_unchanged_valid_stl(tmp_path):
    import time
    from unittest import mock

    class FakeCap:
        def __init__(self):
            self.posts = []

        def post_message(self, message):
            self.posts.append(message)

    def wait_for_result(cap, count=1):
        deadline = time.time() + 5
        while time.time() < deadline:
            results = [post for post in cap.posts if post.get("type") == "plate_result"]
            if len(results) >= count:
                return results[-1]
            time.sleep(0.02)
        raise AssertionError(f"no plate result: {cap.posts}")

    path = tmp_path / "box.stl"
    path.write_bytes(b"valid stl")
    exported = {"file": str(path), "filename": path.name, "format": "stl",
                "size_bytes": path.stat().st_size, "stats": {}, "preview": None,
                "var": "result"}
    base = {"command": "plate", "kind": "code", "code": "result = 1",
            "filename": "box", "tolerance": 0.01, "revision_id": 10}
    cap = FakeCap()
    with mock.patch.object(mod, "run_build123d_code", return_value=exported) as build, \
         mock.patch.object(mod, "_open_with_default_app", return_value=True):
        mod._handle_message_sync(cap, {**base, "request_id": 61})
        first = wait_for_result(cap)
        mod._handle_message_sync(cap, {**base, "request_id": 62})
        second = wait_for_result(cap, 2)
        assert build.call_count == 1
        assert first["reused"] is False and second["reused"] is True

        mod._handle_message_sync(cap, {**base, "request_id": 63, "code": "result = 2"})
        wait_for_result(cap, 3)
        assert build.call_count == 2
        mod._handle_message_sync(cap, {**base, "request_id": 64, "tolerance": 0.02})
        wait_for_result(cap, 4)
        assert build.call_count == 3
        path.unlink()
        mod._handle_message_sync(cap, {**base, "request_id": 65, "tolerance": 0.02})
        wait_for_result(cap, 5)
        assert build.call_count == 4


def test_plate_export_failure_is_distinct_from_open_failure():
    import time
    from unittest import mock

    class FakeCap:
        def __init__(self):
            self.posts = []

        def post_message(self, message):
            self.posts.append(message)

    cap = FakeCap()
    with mock.patch.object(mod, "run_build123d_code", side_effect=PermissionError("exports denied")):
        mod._handle_message_sync(cap, {"command": "plate", "kind": "code", "code": "result = 1",
                                       "filename": "box", "request_id": 65, "revision_id": 11})
        deadline = time.time() + 5
        while time.time() < deadline and not any(p.get("type") == "plate_result" for p in cap.posts):
            time.sleep(0.02)
    result = next(p for p in cap.posts if p.get("type") == "plate_result")
    assert result["ok"] is False and result["export_ok"] is False
    assert result["open_request_sent"] is False
    assert result["handoff_status"] == "export_failed"
    assert "exports denied" in result["error"]


def _load_source_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def test_approximation_metadata_is_bundled_and_only_predefined_objects_warn():
    bundle = _load_source_module("bundle_warnings", ROOT / "packaging" / "bundle.py")
    parsed = bundle.parse_object(ROOT / "objects" / "gridfinity_bin.py")
    assert parsed["warnings"] == [
        "Thumbscrew threads are represented as plain holes; position is exact.",
        "M3 screw threads are not modeled; holes are clearance holes.",
    ]
    assert mod._warnings_for_msg({"kind": "generate", "primitive": "gridfinity_bin"}) == parsed["warnings"]
    assert mod._warnings_for_msg({"kind": "run", "primitive": "gridfinity_bin"}) == []
    assert mod._warnings_for_msg({"kind": "generate", "primitive": "box"}) == []


def test_named_mode_options_are_extracted_and_bundled():
    bundle = _load_source_module("bundle_modes", ROOT / "packaging" / "bundle.py")
    expected = {
        "gridfinity_bin": {
            "HMODE": [(0, "Grid units"), (1, "Interior height"),
                      (2, "Exterior height"), (3, "Exterior height with lip")],
            "TABSTYLE": [(0, "Full"), (1, "Auto"), (2, "Left"),
                         (3, "Center"), (4, "Right"), (5, "None")],
            "TABPLACE": [(0, "Every cell"), (1, "Top-left only")],
        },
        "gridfinity_baseplate": {
            "STYLE": [(0, "Plain"), (1, "Weighted"), (2, "Skeletonized"),
                      (3, "Screw-together"), (4, "Screw-together minimal")],
            "HOLESTYLE": [(0, "Plain"), (1, "Countersunk"), (2, "Counterbored")],
        },
    }
    for primitive, keys in expected.items():
        parsed = bundle.parse_object(ROOT / "objects" / f"{primitive}.py")
        params = {param["key"]: param for param in parsed["params"]}
        for key, options in keys.items():
            actual = [(option["value"], option["label"]) for option in params[key]["options"]]
            assert actual == options
            assert params[key]["default"] == {"TABSTYLE": 1}.get(key, 0)
            bundled = next(p for p in mod.PRIMITIVES[primitive]["params"] if p["key"] == key)
            assert bundled["options"] == params[key]["options"]

    code = _gbin(HMODE=2, TABSTYLE=3, TABPLACE=1)
    assert "HMODE = 2" in code and "TABSTYLE = 3" in code and "TABPLACE = 1" in code
    try:
        mod.validate_primitive_params("gridfinity_bin", dict(
            {p["key"]: p["default"] for p in mod.PRIMITIVES["gridfinity_bin"]["params"]},
            HMODE="Exterior height"))
        raise AssertionError("expected ValueError for a display label submitted as a value")
    except ValueError:
        pass


def test_objects_live_in_their_own_files():
    bundle = _load_source_module("bundle", ROOT / "packaging" / "bundle.py")
    expected = ["box", "bracket", "cylinder", "gridfinity_baseplate",
                "gridfinity_bin", "tube"]
    assert sorted(p.stem for p in (ROOT / "objects").glob("*.py")
                  if p.name != "__init__.py") == expected
    for name in expected:
        # parsed, never imported: object files execute CAD on import
        parsed = bundle.parse_object(ROOT / "objects" / f"{name}.py")
        assert parsed["name"] == name and parsed["label"] and parsed["params"]
        assert "from build123d import" in parsed["source"]
        bundle.smoke_object(parsed)  # defaults + extremes stay valid python
    if str(ROOT) not in sys.path:
        sys.path.insert(0, str(ROOT))
    import objects as registry
    assert sorted(registry.OBJECTS) == expected


def test_bundle_in_sync():
    bundle = _load_source_module("bundle", ROOT / "packaging" / "bundle.py")
    assert bundle.check(), "orcad.py out of sync — run python3 packaging/bundle.py --write"
