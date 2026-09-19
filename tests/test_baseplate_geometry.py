"""Build123d regression checks for screw-together baseplate seam holes."""
import sys
from pathlib import Path

import pytest  # pyright: ignore[reportMissingImports]

ROOT = Path(__file__).resolve().parents[1]
pytest.importorskip("build123d")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import orcad


BASEPLATE = {
    "GX": 1,
    "GY": 1,
    "T": 5,
    "STYLE": 0,
    "HOLESTYLE": 0,
    "DISTX": 0,
    "DISTY": 0,
    "FITX": 0,
    "FITY": 0,
    "SCREW_D": 3.35,
    "SCREW_HEAD": 5,
    "SCREW_SPACING": 0.5,
    "NSCREWS": 1,
    "SOCKETS": False,
    "REFINED": False,
    "MAGNETS": False,
    "SCREW": False,
    "CRUSH": True,
    "CHAMFER": True,
    "PRINTABLE": False,
    "CORNERS": False,
}


def _build_namespace(style=0, nscrews=1, **overrides):
    params = dict(BASEPLATE, STYLE=style, NSCREWS=nscrews)
    params.update(overrides)
    namespace = {}
    exec(orcad.generate_primitive_code("gridfinity_baseplate", params), namespace)
    return namespace


def _build(style=0, nscrews=1, **overrides):
    return _build_namespace(style, nscrews, **overrides)["result"]


def _solid_contains(shape, point):
    return any(solid.is_inside(point) for solid in shape.solids())


@pytest.mark.parametrize("style", (3, 4))
@pytest.mark.parametrize("nscrews", (1, 2, 3))
def test_screw_together_seam_holes_open_inward_on_all_sides(style, nscrews):
    shape = _build(style, nscrews)
    offsets = [
        (i - (nscrews - 1) / 2) * (BASEPLATE["SCREW_HEAD"] + BASEPLATE["SCREW_SPACING"])
        for i in range(nscrews)
    ]
    z = (BASEPLATE["T"] + 6.75) / 2
    points = (
        [("left", (-21 + 2, offset, z)) for offset in offsets]
        + [("right", (21 - 2, offset, z)) for offset in offsets]
        + [("front", (offset, -21 + 2, z)) for offset in offsets]
        + [("back", (offset, 21 - 2, z)) for offset in offsets]
    )
    for side, point in points:
        assert not _solid_contains(shape, point), f"{style=} {nscrews=} {side=}"


@pytest.mark.parametrize("axis", ("x", "y"))
@pytest.mark.parametrize(("fit", "expected"), ((-1, -21), (0, 0), (1, 21)))
def test_fit_to_drawer_keeps_socket_centers_inside_the_slab(axis, fit, expected):
    overrides = {"DISTX": 84, "DISTY": 42, "SOCKETS": True}
    if axis == "y":
        overrides["DISTX"], overrides["DISTY"] = 42, 84
        overrides["FITX"], overrides["FITY"] = 0, fit
    else:
        overrides["FITX"] = fit
    namespace = _build_namespace(**overrides)
    shape = namespace["result"]
    size = namespace["W"] if axis == "x" else namespace["D"]
    point = (expected, 0, 4.5) if axis == "x" else (0, expected, 4.5)

    assert namespace["_PX" if axis == "x" else "_PY"] == pytest.approx(expected)
    socket_half_width = 40.5 / 2
    assert size / 2 - abs(expected) - socket_half_width >= 0.75 - 1e-9
    assert not _solid_contains(shape, point)


def test_fit_to_drawer_moves_cell_holes_with_the_socket_grid():
    namespace = _build_namespace(
        DISTX=84,
        DISTY=84,
        FITX=1,
        FITY=-1,
        SOCKETS=True,
        MAGNETS=True,
        CRUSH=False,
        CHAMFER=False,
    )
    shape = namespace["result"]

    assert not _solid_contains(shape, (21, -21, 4.5))
    for x in (8, 34):
        for y in (-34, -8):
            assert not _solid_contains(shape, (x, y, 4)), (x, y)


def test_corner_only_holes_keep_outer_slab_margins():
    namespace = _build_namespace(
        GX=2,
        GY=2,
        DISTX=126,
        DISTY=126,
        FITX=1,
        FITY=-1,
        SOCKETS=False,
        MAGNETS=True,
        CRUSH=False,
        CHAMFER=False,
        CORNERS=True,
    )
    shape = namespace["result"]
    hx = namespace["W"] / 2 - 7.75
    hy = namespace["D"] / 2 - 7.75

    assert len(namespace["_plate_hole_xy"]()) == 4
    for x in (-hx, hx):
        for y in (-hy, hy):
            assert not _solid_contains(shape, (x, y, 4))
    assert _solid_contains(shape, (34, 34, 4))
