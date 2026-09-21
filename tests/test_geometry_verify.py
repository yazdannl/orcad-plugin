"""Focused, dependency-light checks for the reproducible verification harness."""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from verify import batch_ref, matrix, w_compare


def test_authoritative_matrix_has_all_cases_and_identity():
    assert len(matrix.CASE_MATRIX) == 27
    assert len(batch_ref.CASES) == 27
    for name, case in matrix.CASE_MATRIX.items():
        assert case["name"] == name
        assert case["ours"] and case["reference"]
        assert isinstance(case["features"], list)
        assert matrix.MATRIX[name] == case["ours"]
        assert batch_ref.CASES[name] == case["reference"]


def test_cache_metadata_key_invalidates_parameter_change(tmp_path):
    source = tmp_path / "source.py"
    source.write_text("one")
    before = matrix.source_identity(tmp_path, (".py",))
    source.write_text("two")
    assert matrix.source_identity(tmp_path, (".py",))["fingerprint"] != before["fingerprint"]

    expected = {"schema": 1, "case": "x", "params": {"a": 1},
                "tool": {"version": "one"}, "options": {"fs": 0.25}}
    stl = tmp_path / "x.stl"
    sidecar = tmp_path / "x.json"
    stl.write_bytes(b"stl")
    sidecar.write_text(json.dumps({**expected, "cache_key": matrix.metadata_key(expected)}))
    assert matrix.cache_valid(stl, sidecar, expected)
    changed = {**expected, "params": {"a": 2}}
    assert not matrix.cache_valid(stl, sidecar, changed)
    assert not matrix.cache_valid(stl, tmp_path / "missing.json", expected)


def _hole(x, y, radius):
    return {"center": [x, y], "radius": radius, "area": 3.14 * radius * radius}


def test_holes_are_matched_one_to_one_and_by_size():
    ref = [_hole(0, 0, 2), _hole(0, 0, 2)]
    ours = [_hole(0.1, 0, 2)]
    result = w_compare.match_holes(ref, ours, center_tol=0.4, radius_tol=0.1)
    assert len(result["matches"]) == 1
    assert len(result["missing"]) == 1
    assert not result["extra"]

    wrong_size = w_compare.match_holes([_hole(0, 0, 2)], [_hole(0, 0, 3)],
                                       center_tol=0.4, radius_tol=0.1)
    assert wrong_size["size_mismatch"] == [_hole(0, 0, 2)]
    assert wrong_size["missing"] and wrong_size["extra"]


def test_subprocess_timeout_reports_command_and_limit(tmp_path):
    with pytest.raises(RuntimeError, match=r"command timed out after 0\.01s"):
        matrix.run_command([sys.executable, "-c", "import time; time.sleep(1)"],
                           cwd=tmp_path, timeout=0.01)


def test_cli_configuration_comes_from_environment(monkeypatch, tmp_path):
    monkeypatch.setenv("ORCAD_UPSTREAM", str(tmp_path / "upstream"))
    monkeypatch.setenv("ORCAD_OPENSCAD", "openscad-test")
    monkeypatch.setenv("ORCAD_VERIFY_CACHE", str(tmp_path / "cache"))
    monkeypatch.setenv("ORCAD_SUBPROCESS_TIMEOUT", "12")
    args = batch_ref.parse_args([])
    assert args.upstream == tmp_path / "upstream"
    assert args.openscad == "openscad-test"
    assert args.cache == tmp_path / "cache"
    assert args.timeout == 12


def test_driver_root_is_explicit_and_repository_relative(tmp_path):
    source = "$fa = 4;\n$fs = 0.25;\ngridx = 2;\n"
    (tmp_path / "gridfinity-rebuilt-bins.scad").write_text(source)
    driver = __import__("verify.w_scad", fromlist=["build_driver"]).build_driver(
        "bins", {"gridx": 3}, root=tmp_path)
    assert "gridx = 3;" in driver
    assert str(ROOT) not in driver
