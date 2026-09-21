"""Optional build123d integration regressions for every predefined object.

Run explicitly with ``python3 verify/geometry.py``.  These tests use the
project's .venv and are deliberately separate from pure plugin tests.
"""
from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

pytestmark = pytest.mark.cad

from tests.geometry_support import require_project_build123d

build123d = require_project_build123d()
from build123d import Plane, section  # noqa: E402

import orcad  # noqa: E402


DEFAULT_DIMENSIONS = {
    "box": (20.0, 20.0, 20.0),
    "bracket": (60.0, 30.0, 5.0),
    "cylinder": (20.0, 20.0, 20.0),
    "tube": (24.0, 24.0, 25.0),
    "gridfinity_baseplate": (168.0, 168.0, 5.0),
    "gridfinity_bin": (83.5, 83.5, 45.55),
}


def _build_default(primitive):
    params = orcad._defaults(primitive)
    try:
        return orcad._execute_code(orcad.generate_primitive_code(primitive, params))[0]
    except Exception as exc:
        pytest.fail(f"{primitive} defaults failed to build: {type(exc).__name__}: {exc}")


@pytest.fixture(scope="module")
def default_shapes():
    return {primitive: _build_default(primitive) for primitive in DEFAULT_DIMENSIONS}


def _assert_valid_solid(shape, context):
    valid = getattr(shape, "is_valid", False)
    if callable(valid):
        valid = valid()
    assert valid, f"{context} produced an invalid build123d shape"
    solids = shape.solids()
    assert len(solids) == 1, f"{context} produced {len(solids)} solids, expected one"
    assert shape.volume > 0, f"{context} has zero volume"


def _size(shape):
    return tuple(float(value) for value in shape.bounding_box().size)


def test_every_default_object_is_one_valid_solid_with_expected_dimensions(default_shapes):
    for primitive, expected in DEFAULT_DIMENSIONS.items():
        shape = default_shapes[primitive]
        _assert_valid_solid(shape, f"{primitive} default parameters {orcad._defaults(primitive)}")
        assert _size(shape) == pytest.approx(expected, abs=0.03), (
            f"{primitive} default dimensions changed: got {_size(shape)}, expected {expected}"
        )


@pytest.mark.parametrize(
    ("primitive", "z", "expected_area"),
    (
        ("box", 0.0, 400.0),
        ("cylinder", 0.0, 100.0 * 3.141592653589793),
        ("tube", 0.0, (12.0**2 - 8.0**2) * 3.141592653589793),
        ("bracket", 0.0, 60.0 * 30.0 - 2.0 * 2.5**2 * 3.141592653589793),
    ),
)
def test_simple_default_cross_sections_are_meaningful(default_shapes, primitive, z, expected_area):
    shape = default_shapes[primitive]
    measured = section(shape, Plane.XY, z).area
    assert measured == pytest.approx(expected_area, rel=1e-6, abs=0.01), (
        f"{primitive} cross-section at z={z} changed: got {measured:.4f} mm²"
    )


def test_baseplate_and_bin_cross_sections_preserve_feature_geometry(default_shapes):
    baseplate = default_shapes["gridfinity_baseplate"]
    bottom = section(baseplate, Plane.XY, 0.1).area
    socket_level = section(baseplate, Plane.XY, 2.5).area
    assert 20_000 < bottom < 30_000, f"baseplate bottom section changed: {bottom:.3f} mm²"
    assert 5_000 < socket_level < 8_000, (
        f"baseplate socket section changed: {socket_level:.3f} mm²"
    )
    assert bottom > socket_level * 2, "baseplate sockets no longer remove the expected material"

    bin_shape = default_shapes["gridfinity_bin"]
    wall_section = section(bin_shape, Plane.XY, 20.0).area
    lip_section = section(bin_shape, Plane.XY, 40.0).area
    assert 350 < wall_section < 550, f"bin wall section changed: {wall_section:.3f} mm²"
    assert lip_section > wall_section, "bin stacking lip section disappeared"


def test_wall_thickness_is_visible_in_a_stable_cross_section():
    base = dict(orcad._defaults("gridfinity_bin"), GX=1, GY=1, LIP=False, TABSTYLE=5)
    sections = {}
    for wall in (0.95, 2.4):
        params = dict(base, WALL=wall)
        try:
            shape, _ = orcad._execute_code(orcad.generate_primitive_code("gridfinity_bin", params))
        except Exception as exc:
            pytest.fail(f"gridfinity_bin WALL={wall} failed to build: {type(exc).__name__}: {exc}")
        _assert_valid_solid(shape, f"gridfinity_bin WALL={wall}")
        sections[wall] = section(shape, Plane.XY, 20.0).area
    assert sections[2.4] > sections[0.95] * 1.8, (
        f"wall cross-section did not grow with thickness: {sections}"
    )


@pytest.mark.parametrize("export_format", ("stl", "step", "3mf"))
def test_default_box_exports_to_every_supported_format(tmp_path, export_format):
    code = orcad.generate_primitive_code("box", orcad._defaults("box"))
    with mock.patch.object(orcad, "exports_dir", return_value=tmp_path):
        try:
            result = orcad.run_build123d_code(code, export_format, 0.02, "geometry-regression")
        except Exception as exc:
            pytest.fail(
                f"box default {export_format} export failed; code parameters "
                f"{orcad._defaults('box')}: {type(exc).__name__}: {exc}"
            )
    output = Path(result["file"])
    assert result["ok"] is True
    assert result["format"] == export_format
    assert output.suffix == f".{export_format}"
    assert output.is_file() and output.stat().st_size > 0
    assert result["stats"]["dimensions_mm"] == pytest.approx(
        {"width": 20.0, "depth": 20.0, "height": 20.0}, abs=0.03
    )


def test_public_helpers_report_invalid_params_unsupported_format_and_bad_solid():
    with pytest.raises(ValueError, match="R_IN.*smaller than R_OUT"):
        orcad.generate_primitive_code("tube", {"R_OUT": 8, "R_IN": 8, "H": 10})

    with pytest.raises(ValueError, match="format must be one of"):
        orcad.run_build123d_code("result = Box(2, 2, 2)", "obj")

    with pytest.raises(RuntimeError, match="zero volume|solid"):
        orcad.run_build123d_code("result = Box(2, 2, 0)", "stl")


def test_public_export_helper_reports_write_failure_and_removes_partial_output(tmp_path):
    code = orcad.generate_primitive_code("box", orcad._defaults("box"))
    with (
        mock.patch.object(orcad, "exports_dir", return_value=tmp_path),
        mock.patch.object(build123d, "export_stl", side_effect=OSError("simulated disk full")),
        pytest.raises(RuntimeError, match="Export to stl failed"),
    ):
        orcad.run_build123d_code(code, "stl", filename_stem="write-failure")
    assert not list(tmp_path.iterdir()), "failed export left a partial CAD artifact"
