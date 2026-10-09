"""OpenSCAD backend: catalog, validation, argv, logs, meshes and the runner."""
from __future__ import annotations

import json
import os
import re
import struct
import threading
import time
import zipfile
from pathlib import Path

import pytest

from openscad import (CATALOG, BackendError, OpenSCADRunner, backend_args, build_argv, clean_log, defaults,
                      encode_define, index_stl, probe_openscad, source_path, triangle_count,
                      validate_parameters, write_3mf)
from openscad.catalog import LIBRARIES, LIBRARY_DIRS, LIBRARY_SEARCH_PATH, ROOT
from openscad.errors import ErrorCode
from openscad.runner import _failure, child_env
from fakes import fake_command

CUBE = [((0, 0, 0), (1, 1, 0), (1, 0, 0)), ((0, 0, 0), (0, 1, 0), (1, 1, 0)),
        ((0, 0, 1), (1, 0, 1), (1, 1, 1)), ((0, 0, 1), (1, 1, 1), (0, 1, 1)),
        ((0, 0, 0), (1, 0, 0), (1, 0, 1)), ((0, 0, 0), (1, 0, 1), (0, 0, 1)),
        ((0, 1, 0), (0, 1, 1), (0, 0, 1)), ((0, 1, 0), (0, 0, 1), (0, 0, 0)),
        ((0, 0, 0), (0, 0, 1), (0, 1, 1)), ((0, 0, 0), (0, 1, 1), (0, 1, 0)),
        ((1, 0, 0), (1, 1, 0), (1, 1, 1)), ((1, 0, 0), (1, 1, 1), (1, 0, 1))]
INCLUDE_RE = re.compile(r"\b(?:include|use)\s*<([^>]+)>")


def stl(triangles=CUBE) -> bytes:
    out = bytearray(b"test".ljust(80, b"\0") + struct.pack("<I", len(triangles)))
    for tri in triangles:
        out += struct.pack("<12fH", 0, 0, 0, *tri[0], *tri[1], *tri[2], 0)
    return bytes(out)


def _scad_text(path: Path) -> str:
    """.scad source without comments, so prose about an include is not mistaken for one."""
    text = re.sub(r"/\*.*?\*/", "", path.read_text(encoding="utf-8", errors="replace"), flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


# ------------------------------------------------------------------ catalog

def test_every_catalog_object_is_complete_and_its_defaults_validate():
    assert {"gridfinity_bin", "gridfinity_baseplate", "box", "cylinder", "tube", "bracket"} <= set(CATALOG["objects"])
    for name, spec in CATALOG["objects"].items():
        assert source_path(name).is_file(), name
        assert spec["label"] and spec["category"] and spec["icon"] and spec["description"]
        for param in spec["parameters"]:
            assert param["type"] in ("boolean", "integer", "number")
            assert param["variable"] not in ("$fa", "$fs")
        assert validate_parameters(name, {}) == defaults(name)


def test_every_object_has_a_short_name_and_a_preview_thumbnail():
    """The list item shows `short` and its thumbnail; both are generated data."""
    thumbs = Path(__file__).resolve().parents[1] / "frontend" / "src" / "thumbs"
    short_names = set()
    for name, spec in CATALOG["objects"].items():
        short = spec["short"]
        assert short.strip() == short and 0 < len(short) <= 20, (name, short)
        assert short.casefold() not in short_names, f"duplicate short name: {short}"
        short_names.add(short.casefold())
        thumbnail = thumbs / f"{name}.png"
        assert thumbnail.is_file(), f"{name} has no thumbnail; run `xvfb-run -a python3 dev/thumbs.py`"
        assert 100 < thumbnail.stat().st_size < 40_000, name


def test_every_object_documents_itself_and_its_presets_validate():
    for name, spec in CATALOG["objects"].items():
        variables = {param["variable"] for param in spec["parameters"]}
        assert spec["parameters"], f"{name} has no parameters"
        assert spec["tags"] and all(tag.strip() for tag in spec["tags"]), name
        assert spec["description"].strip(), name
        for preset in spec["presets"]:
            assert preset["name"].strip(), name
            assert set(preset["params"]) <= variables, (name, preset["name"])
            assert validate_parameters(name, preset["params"]) == {**defaults(name), **preset["params"]}


# ---------------------------------------------------------------- libraries

def _revision_metadata(tree: Path) -> dict[str, str]:
    """Upstream provenance the REVISION files carry next to the pinned commit."""
    lines = (tree / "REVISION").read_text(encoding="utf-8").splitlines()[1:]
    return dict(line.split(": ", 1) for line in lines if not line.startswith((" ", "\t")) and ": " in line)


def test_every_library_is_pinned_to_its_revision_and_keeps_its_license():
    assert list(LIBRARIES) == list(LIBRARY_DIRS), "the search path must follow the catalog order"
    assert LIBRARY_SEARCH_PATH[:-1] == list(LIBRARY_DIRS.values()), "gridfinity-rebuilt has to come first"
    assert LIBRARY_SEARCH_PATH[-1] == ROOT / "vendor", "vendor/ itself addresses one library as <root/file>"
    for key, spec in LIBRARIES.items():
        tree = LIBRARY_DIRS[key]
        assert tree.is_dir(), key
        assert re.fullmatch(r"[0-9a-f]{40}", spec["revision"]), key
        revision = (tree / "REVISION").read_text(encoding="utf-8")
        assert revision.splitlines()[0].strip() == spec["revision"], f"{key} REVISION disagrees with the catalog"
        assert any((tree / name).is_file() for name in ("LICENSE", "LICENCE", "LICENSE.txt", "COPYING")), key
        assert spec["label"] and spec["license"] and spec["repository"].startswith("https://"), key
        metadata = _revision_metadata(tree)
        assert spec["license"] == metadata.get("License", spec["license"]), key
        assert spec["repository"] == metadata.get("Upstream", spec["repository"]), key


def test_every_library_is_reachable_from_the_catalog():
    """Nothing vendored is dead weight, and no object points outside the backend tree."""
    reached = {key: 0 for key in LIBRARIES}
    for name in CATALOG["objects"]:
        source = source_path(name)
        owner = next((key for key, root in LIBRARY_DIRS.items() if source.is_relative_to(root)), None)
        assert owner is not None or source.is_relative_to(ROOT / "objects"), name
        if owner is not None:
            reached[owner] += 1
        for target in INCLUDE_RE.findall(_scad_text(source)):
            # OpenSCAD looks next to the including file first, then on every OPENSCADPATH root.
            found = next((path for path in (source.parent / target, *(root / target for root in LIBRARY_SEARCH_PATH))
                          if path.exists()), None)
            for key, tree in LIBRARY_DIRS.items():
                if found is not None and found.is_relative_to(tree):
                    reached[key] += 1
    assert set(reached) == set(LIBRARIES)
    assert all(reached.values()), reached


# -------------------------------------------------------------- .scad sources

def test_every_parameter_is_used_in_its_source_and_every_include_resolves():
    for name, spec in CATALOG["objects"].items():
        source = source_path(name)
        text = _scad_text(source)
        for param in spec["parameters"]:
            assert re.search(rf"\b{re.escape(param['variable'])}\b", text), f"{name}.{param['variable']}"
        for target in INCLUDE_RE.findall(text):
            # OpenSCAD looks next to the including file first, then on every OPENSCADPATH root.
            assert any((root / target).exists() for root in (source.parent, *LIBRARY_SEARCH_PATH)), \
                f"{name} includes {target}"


def test_validation_is_strict_and_names_the_offending_fields():
    cases = [
        ("box", {"length": True}),
        ("box", {"length": "20"}),
        ("box", {"length": float("nan")}),
        ("box", {"length": 0.5}),
        ("box", {"length": 20.25}),  # off-step
        ("box", {"nope": 1}),
        ("gridfinity_bin", {"gridx": 2.0}),  # integer parameter given a float
        ("gridfinity_bin", {"style_tab": 9}),
    ]
    for name, params in cases:
        with pytest.raises(BackendError) as info:
            validate_parameters(name, params)
        assert info.value.code == ErrorCode.INVALID_PARAMETERS
        assert info.value.details["fields"], params


@pytest.mark.parametrize("name, params, fields", [
    ("tube", {"inner_diameter": 30}, {"inner_diameter", "outer_diameter"}),
    ("bracket", {"hole_spacing": 58}, {"hole_spacing", "hole_diameter", "length"}),
    ("gridfinity_bin", {"magnet_holes": True, "refined_holes": True},
     {"refined_holes", "magnet_holes"}),
    ("gridfinity_bin", {"divx": 0}, {"divx", "divy"}),
    ("gridfinity_baseplate", {"gridx": 0}, {"gridx", "distancex"}),
])
def test_cross_field_rules(name, params, fields):
    with pytest.raises(BackendError) as info:
        validate_parameters(name, params)
    assert set(info.value.details["fields"]) == fields


# --------------------------------------------------------------------- argv

def test_defines_are_separate_safe_arguments():
    assert encode_define("gridx", 2) == "gridx=2"
    assert encode_define("scoop", 0.5) == "scoop=0.5"
    assert encode_define("include_lip", False) == "include_lip=false"
    assert encode_define("$fa", 6.0) == "$fa=6"
    for name, value in (("x; y", 1), ("gridx", "2; rm -rf /"), ("gridx", float("inf")), ("gridx", None)):
        with pytest.raises(ValueError):
            encode_define(name, value)


def test_argv_requests_binary_stl_quality_and_the_fast_kernel():
    argv = build_argv("openscad", Path("in dir/model.scad"), Path("out dir/x.stl"), {"gridx": 2}, "draft", "2026.09.22")
    assert argv[:5] == ["openscad", "-o", str(Path("out dir/x.stl")), "--export-format", "binstl"]
    assert "--backend=Manifold" in argv
    assert argv[argv.index("gridx=2") - 1] == "-D"
    assert "$fa=12" in argv and "$fs=0.8" in argv
    assert argv[-1] == str(Path("in dir/model.scad"))
    assert backend_args("2023.09.11") == ["--enable=manifold"]
    assert backend_args("2021.01") == [] and backend_args(None) == []
    with pytest.raises(BackendError):
        build_argv("openscad", Path("m.scad"), Path("x.stl"), {}, "ultra", "2026.01.01")


def test_child_env_drops_host_library_paths_and_exposes_the_library(monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/tmp/.mount_orca/usr/lib")
    monkeypatch.setenv("PYTHONHOME", "/orca/python")
    env = child_env()
    assert "LD_LIBRARY_PATH" not in env and "PYTHONHOME" not in env
    roots = env["OPENSCADPATH"].split(os.pathsep)
    assert roots == [str(path) for path in LIBRARY_SEARCH_PATH]
    assert roots[0] == str(LIBRARY_DIRS["gridfinity-rebuilt-openscad"]), "gridfinity-rebuilt resolves src/... includes"
    assert all(Path(root).is_dir() for root in roots), roots
    assert Path(roots[0]).joinpath("src", "core", "standard.scad").is_file()


def test_logs_are_cleaned_and_failures_are_readable(tmp_path):
    source = tmp_path / "model.scad"
    log = clean_log("Geometries in cache: 1\nECHO: 42\n   Facets: 6\n"
                    f"ERROR: Parser error: syntax error in file {source}, line 3\n"
                    f"Can't parse file '{source}'!\n", source)
    assert log == ["ECHO: 42", "ERROR: Parser error: syntax error in file your code, line 3",
                   "Can't parse file your code!"]
    error = _failure(log, 1)
    assert error.message == "Parser error: syntax error on line 3"
    assert error.details["line"] == 3 and error.details["log"] == log
    assert _failure(["Current top level object is empty."], 1).message == "The model is empty"
    assert "2D" in _failure(["Current top level object is not a 3D object."], 1).message


# ------------------------------------------------------------------- meshes

def test_index_stl_deduplicates_vertices():
    mesh = index_stl(stl())
    assert len(mesh.positions) == 8 * 3 and mesh.triangle_count == 12
    assert sorted(set(mesh.indices)) == list(range(8))


def test_triangle_count_rejects_empty_and_truncated_files():
    assert triangle_count(stl()) == 12
    for data in (stl([]), stl()[:-10], b"short"):
        with pytest.raises(BackendError):
            triangle_count(data)


def test_3mf_is_a_valid_package_without_degenerate_triangles(tmp_path):
    degenerate = ((0, 0, 0), (0, 0, 0), (1, 0, 0))
    path = tmp_path / "cube.3mf"
    write_3mf(index_stl(stl(CUBE + [degenerate])), path)
    with zipfile.ZipFile(path) as archive:
        assert {"[Content_Types].xml", "_rels/.rels", "3D/3dmodel.model"} == set(archive.namelist())
        model = archive.read("3D/3dmodel.model").decode()
    assert 'unit="millimeter"' in model
    assert model.count("<vertex ") == 8 and model.count("<triangle ") == 12


# ------------------------------------------------------------------- runner

def fake_openscad(tmp_path: Path, version="2026.09.22", delay=0.0, stderr="", fail=False) -> Path:
    return fake_command(tmp_path, "fake-openscad",
        "import json, os, pathlib, sys, time\n"
        f"if '--version' in sys.argv:\n    print('OpenSCAD version {version}'); raise SystemExit(0)\n"
        "log = pathlib.Path(__file__).with_name('calls.jsonl')\n"
        "with log.open('a') as f: f.write(json.dumps({'argv': sys.argv, 'cwd': os.getcwd(),"
        " 'env': {k: os.environ.get(k) for k in ('OPENSCADPATH', 'LD_LIBRARY_PATH')}}) + '\\n')\n"
        f"time.sleep({delay})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"if {fail}: raise SystemExit(1)\n"
        f"pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_bytes({stl()!r})\n")


def calls(tmp_path):
    path = tmp_path / "calls.jsonl"
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def test_render_object_runs_once_then_serves_the_cache(tmp_path, monkeypatch):
    monkeypatch.setenv("LD_LIBRARY_PATH", "/host/lib")
    runner = OpenSCADRunner(fake_openscad(tmp_path, stderr="ECHO: hello\n"), cache_dir=tmp_path / "cache")
    first = runner.render_object("box", {"length": 30}, "draft")
    assert (first.triangles, first.cached, first.log) == (12, False, ["ECHO: hello"])
    call = calls(tmp_path)[0]
    assert "length=30" in call["argv"] and call["env"]["LD_LIBRARY_PATH"] is None
    assert Path(call["cwd"]) == source_path("box").parent
    second = runner.render_object("box", {"length": 30}, "draft")
    assert second.cached and second.stl == first.stl and second.log == ["ECHO: hello"]
    assert len(calls(tmp_path)) == 1
    runner.render_object("box", {"length": 30}, "final")
    assert len(calls(tmp_path)) == 2


def test_render_code_reports_the_openscad_error(tmp_path):
    runner = OpenSCADRunner(fake_openscad(tmp_path, fail=True, stderr="ERROR: Parser error in file x.scad, line 2\n"),
                            cache_dir=tmp_path / "cache")
    with pytest.raises(BackendError) as info:
        runner.render_code("cube([1,")
    assert info.value.code == ErrorCode.PROCESS_FAILED
    assert info.value.message == "Parser error on line 2" and info.value.details["line"] == 2
    with pytest.raises(BackendError):
        runner.render_code("   ")
    assert not list((tmp_path / "cache").glob("*.stl"))


def test_timeout_and_cancel_kill_the_process(tmp_path):
    runner = OpenSCADRunner(fake_openscad(tmp_path, delay=5), cache_dir=tmp_path / "cache")
    started = time.monotonic()
    with pytest.raises(BackendError) as info:
        runner.render_object("box", {}, timeout=0.3)
    assert info.value.code == ErrorCode.TIMEOUT
    cancel = threading.Event()
    threading.Timer(0.2, cancel.set).start()
    with pytest.raises(BackendError) as info:
        runner.render_object("cylinder", {}, cancel=cancel.is_set)
    assert info.value.code == ErrorCode.CANCELLED
    assert time.monotonic() - started < 4


def test_old_openscad_is_rejected(tmp_path):
    assert not probe_openscad(fake_openscad(tmp_path, "2021.01")).supported
    with pytest.raises(BackendError) as info:
        OpenSCADRunner(fake_openscad(tmp_path, "2021.01"), cache_dir=tmp_path)
    assert info.value.code == ErrorCode.UNSUPPORTED_VERSION


def test_cache_is_pruned_oldest_first(tmp_path):
    runner = OpenSCADRunner(fake_openscad(tmp_path), cache_dir=tmp_path / "cache", cache_limit=len(stl()) * 2)
    for length in (10, 11, 12):
        runner.render_object("box", {"length": length})
        time.sleep(0.02)
    assert len(list((tmp_path / "cache").glob("*.stl"))) == 2


def _real_openscad():
    try:
        return probe_openscad().supported
    except BackendError:
        return False


@pytest.mark.skipif(not _real_openscad(), reason="needs OpenSCAD 2023+ on PATH")
def test_real_openscad_renders_every_catalog_object(tmp_path):
    runner = OpenSCADRunner(cache_dir=tmp_path)
    for name in CATALOG["objects"]:
        assert runner.render_object(name, {}, "draft").triangles > 0, name
    assert runner.render_code("include <src/core/standard.scad>\ncube(GRID_DIMENSIONS_MM.x);").triangles == 12
