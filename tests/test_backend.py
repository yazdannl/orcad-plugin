"""OpenSCAD backend: catalog, validation, argv, logs, meshes and the runner."""
from __future__ import annotations

import json
import stat
import struct
import sys
import threading
import time
import zipfile
from pathlib import Path

import pytest

from openscad import (CATALOG, BackendError, OpenSCADRunner, backend_args, build_argv, clean_log, defaults,
                      encode_define, index_stl, probe_openscad, source_path, triangle_count,
                      validate_parameters, write_3mf)
from openscad.errors import ErrorCode
from openscad.runner import _failure, child_env

CUBE = [((0, 0, 0), (1, 1, 0), (1, 0, 0)), ((0, 0, 0), (0, 1, 0), (1, 1, 0)),
        ((0, 0, 1), (1, 0, 1), (1, 1, 1)), ((0, 0, 1), (1, 1, 1), (0, 1, 1)),
        ((0, 0, 0), (1, 0, 0), (1, 0, 1)), ((0, 0, 0), (1, 0, 1), (0, 0, 1)),
        ((0, 1, 0), (0, 1, 1), (0, 0, 1)), ((0, 1, 0), (0, 0, 1), (0, 0, 0)),
        ((0, 0, 0), (0, 0, 1), (0, 1, 1)), ((0, 0, 0), (0, 1, 1), (0, 1, 0)),
        ((1, 0, 0), (1, 1, 0), (1, 1, 1)), ((1, 0, 0), (1, 1, 1), (1, 0, 1))]


def stl(triangles=CUBE) -> bytes:
    out = bytearray(b"test".ljust(80, b"\0") + struct.pack("<I", len(triangles)))
    for tri in triangles:
        out += struct.pack("<12fH", 0, 0, 0, *tri[0], *tri[1], *tri[2], 0)
    return bytes(out)


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
    ("gridfinity_bin", {"magnet_holes": True}, {"refined_holes", "magnet_holes"}),
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
    assert Path(env["OPENSCADPATH"]).joinpath("src", "core", "standard.scad").is_file()


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
    script = tmp_path / "fake-openscad"
    script.write_text(
        f"#!{sys.executable}\n"
        "import json, os, pathlib, sys, time\n"
        f"if '--version' in sys.argv:\n    print('OpenSCAD version {version}'); raise SystemExit(0)\n"
        "log = pathlib.Path(__file__).with_name('calls.jsonl')\n"
        "with log.open('a') as f: f.write(json.dumps({'argv': sys.argv, 'cwd': os.getcwd(),"
        " 'env': {k: os.environ.get(k) for k in ('OPENSCADPATH', 'LD_LIBRARY_PATH')}}) + '\\n')\n"
        f"time.sleep({delay})\n"
        f"sys.stderr.write({stderr!r})\n"
        f"if {fail}: raise SystemExit(1)\n"
        f"pathlib.Path(sys.argv[sys.argv.index('-o') + 1]).write_bytes({stl()!r})\n",
        encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


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
