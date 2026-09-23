"""Focused tests for the standard-library OpenSCAD/Gridfinity backend."""
from __future__ import annotations

import json
import os
import stat
import sys
import threading
import time
from pathlib import Path

import pytest

from openscad import (
    CATALOG,
    RenderRequest,
    OpenSCADRunner,
    build_argv,
    cache_key,
    defaults,
    encode_binary,
    encode_define,
    mesh_stats,
    parse_binary,
    probe_openscad,
    validate_parameters,
)


def test_catalog_has_both_entries_and_no_quality_variables_as_parameters():
    assert {"bin", "baseplate"} == set(CATALOG["objects"])
    for spec in CATALOG["objects"].values():
        variables = {item["variable"] for item in spec["parameters"]}
        assert "$fa" not in variables and "$fs" not in variables
        assert variables
        assert all(item["label"] and item["type"] and "default" in item for item in spec["parameters"])


def test_defaults_are_valid_and_validation_is_strict():
    for name in CATALOG["objects"]:
        assert validate_parameters(name, defaults(name)) == defaults(name)
    with pytest.raises(Exception):
        validate_parameters("bin", {"gridx": float("nan")})
    with pytest.raises(Exception):
        validate_parameters("bin", {"gridx": True})
    with pytest.raises(Exception):
        validate_parameters("bin", {"refined_holes": True, "magnet_holes": True})
    with pytest.raises(Exception):
        validate_parameters("baseplate", {"gridx": 0, "gridy": 1, "distancex": 0})


def test_argv_uses_separate_safe_define_arguments():
    argv = build_argv("openscad", "bin", {"gridx": 2}, Path("folder with spaces") / "out.stl", quality_profile="draft")
    assert "-D" in argv
    assert "gridx=2" in argv
    assert "$fa=12" in argv and "$fs=0.8" in argv
    assert argv[3:5] == ["--export-format", "binstl"]
    assert "folder with spaces/out.stl" in argv
    with pytest.raises(ValueError):
        encode_define("gridx", "2; rm -rf /")


def test_cache_key_is_deterministic_and_includes_all_identity_inputs():
    params = defaults("bin")
    first = cache_key("bin", params, "2024.01", "balanced")
    assert first == cache_key("bin", dict(reversed(list(params.items()))), "2024.01", "balanced")
    assert first != cache_key("bin", params, "2024.01", "final")
    assert first != cache_key("bin", params, "2025.01", "balanced")
    assert first != cache_key("baseplate", defaults("baseplate"), "2024.01", "balanced")


def _cube_stl() -> bytes:
    triangles = [
        ((0, 0, 0), (1, 1, 0), (1, 0, 0)), ((0, 0, 0), (0, 1, 0), (1, 1, 0)),
        ((0, 0, 1), (1, 0, 1), (1, 1, 1)), ((0, 0, 1), (1, 1, 1), (0, 1, 1)),
        ((0, 0, 0), (1, 0, 0), (1, 0, 1)), ((0, 0, 0), (1, 0, 1), (0, 0, 1)),
        ((0, 1, 0), (0, 1, 1), (0, 0, 1)), ((0, 1, 0), (0, 0, 1), (0, 0, 0)),
        ((0, 0, 0), (0, 0, 1), (0, 1, 1)), ((0, 0, 0), (0, 1, 1), (0, 1, 0)),
        ((1, 0, 0), (1, 1, 0), (1, 1, 1)), ((1, 0, 0), (1, 1, 1), (1, 0, 1)),
    ]
    from openscad.stl import encode_binary
    return encode_binary(triangles)


def test_binary_stl_codec_compacts_vertices_and_reports_safe_volume():
    mesh = parse_binary(_cube_stl())
    stats = mesh_stats(mesh)
    assert stats["vertex_count"] == 8
    assert stats["triangle_count"] == 12
    assert stats["bbox"] == {"min": [0.0, 0.0, 0.0], "max": [1.0, 1.0, 1.0]}
    assert stats["volume_mm3"] == pytest.approx(1.0)
    assert json.loads(json.dumps(mesh.to_dict()))["triangle_count"] == 12


def _fake_openscad(tmp_path: Path, version: str = "2024.01", delay: float = 0.0) -> Path:
    payload = repr(_cube_stl())
    script = tmp_path / "fake-openscad.py"
    script.write_text(
        "#!" + sys.executable + "\n"
        "import pathlib, sys, time\n"
        f"payload = {payload}\n"
        "if '--version' in sys.argv:\n"
        f"    print('OpenSCAD version {version}')\n"
        "    raise SystemExit(0)\n"
        f"time.sleep({delay})\n"
        "output = pathlib.Path(sys.argv[sys.argv.index('-o') + 1])\n"
        "output.write_bytes(payload)\n",
        encoding="utf-8",
    )
    script.chmod(script.stat().st_mode | stat.S_IXUSR)
    return script


def test_fake_executable_probe_and_render_are_json_serializable(tmp_path):
    runner = OpenSCADRunner(_fake_openscad(tmp_path))
    destination = tmp_path / "result.stl"
    result = runner.render(RenderRequest("bin", defaults("bin"), destination, "draft", 5))
    assert result.ok and destination.is_file()
    assert result.stats["volume_mm3"] == pytest.approx(1.0)
    json.dumps(result.to_dict())


def test_opt_in_cache_reuses_valid_mesh_without_spawning_again(tmp_path):
    executable = _fake_openscad(tmp_path)
    runner = OpenSCADRunner(executable, cache_dir=tmp_path / "cache")
    first = runner.render(RenderRequest("bin", defaults("bin"), tmp_path / "first.stl", "draft", 5))
    assert first.ok and first.argv
    executable.chmod(0o600)
    second = runner.render(RenderRequest("bin", defaults("bin"), tmp_path / "second.stl", "draft", 5))
    assert second.ok and second.argv == ()
    assert second.cache_key == first.cache_key
    assert (tmp_path / "second.stl").read_bytes() == (tmp_path / "first.stl").read_bytes()


def test_2021_01_is_flagged_and_render_is_rejected(tmp_path):
    info = probe_openscad(_fake_openscad(tmp_path, "2021.01"))
    assert not info.supported
    result = OpenSCADRunner(_fake_openscad(tmp_path, "2021.01")).render(
        RenderRequest("bin", defaults("bin"), tmp_path / "no.stl"))
    assert not result.ok
    assert result.error["code"] == "openscad_unsupported_version"


def test_timeout_and_pre_cancelled_request_are_stable(tmp_path):
    result = OpenSCADRunner(_fake_openscad(tmp_path, delay=0.2)).render(
        RenderRequest("bin", defaults("bin"), tmp_path / "slow.stl", timeout=0.01))
    assert not result.ok
    assert result.error["code"] == "timeout"


def test_pre_cancelled_request_does_not_run_process(tmp_path):
    event = threading.Event()
    event.set()
    result = OpenSCADRunner(_fake_openscad(tmp_path)).render(
        RenderRequest("bin", defaults("bin"), tmp_path / "no.stl"), cancel=event)
    assert not result.ok
    assert result.error["code"] == "cancelled"


def test_render_latest_cancels_the_obsolete_process(tmp_path):
    runner = OpenSCADRunner(_fake_openscad(tmp_path, delay=0.3))
    first_result = []

    def render_first():
        first_result.append(runner.render(
            RenderRequest("bin", defaults("bin"), tmp_path / "first.stl", timeout=5)))

    thread = threading.Thread(target=render_first)
    thread.start()
    time.sleep(0.05)
    second = runner.render_latest(
        RenderRequest("bin", defaults("bin"), tmp_path / "second.stl", timeout=5))
    thread.join(timeout=5)

    assert second.ok
    assert first_result and not first_result[0].ok
    assert first_result[0].error["code"] == "cancelled"


@pytest.mark.skipif(not __import__("shutil").which("openscad"), reason="OpenSCAD is not installed")
def test_optional_local_openscad_probe():
    """Only runs on a machine with an OpenSCAD executable."""
    info = probe_openscad()
    if not info.supported:
        pytest.skip(info.warning or "OpenSCAD version is unsupported")
