"""Build123d regressions for Gridfinity bin wall thickness."""

import sys
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tests.geometry_support import require_project_build123d

require_project_build123d()
import orcad


BIN = {
    "GX": 1,
    "GY": 1,
    "HU": 4,
    "HMODE": 0,
    "ZS": False,
    "FILL": 0,
    "DX": 1,
    "DY": 1,
    "DEPTH": 0,
    "SCOOPW": 0,
    "TABSTYLE": 5,
    "TABPLACE": 0,
    "CYL": False,
    "CD": 10,
    "CCHAM": 0.5,
    "REFINED": False,
    "MAGNETS": False,
    "SCREW": False,
    "CRUSH": True,
    "CHAMFER": True,
    "PRINTABLE": False,
    "CORNERS": False,
    "THUMB": False,
    "LIP": False,
}


def _build(wall):
    namespace = {}
    params = dict(BIN, WALL=wall)
    exec(orcad.generate_primitive_code("gridfinity_bin", params), namespace)  # noqa: S102
    return namespace["result"]


def _contains(shape, point):
    return any(solid.is_inside(point) for solid in shape.solids())


def test_wall_changes_the_body_cross_section():
    thin = _build(0.95)
    thick = _build(2.4)

    # At z=20 the 0.95mm bin is already in its compartment, while the
    # 2.4mm bin still has its requested outer wall at this x position.
    assert not _contains(thin, (19.0, 0, 20.0))
    assert _contains(thick, (19.0, 0, 20.0))


def test_wall_changes_finished_bin_volume():
    thin = _build(0.95)
    thick = _build(2.4)

    assert thin.bounding_box().size == pytest.approx(thick.bounding_box().size)
    assert thick.volume > thin.volume
    assert thick.volume != pytest.approx(thin.volume)
