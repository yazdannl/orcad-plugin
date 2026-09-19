"""Build123d regression checks for screw-together baseplate seam holes."""
import sys
from pathlib import Path

import pytest

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


def _build(style, nscrews):
    params = dict(BASEPLATE, STYLE=style, NSCREWS=nscrews)
    namespace = {}
    exec(orcad.generate_primitive_code("gridfinity_baseplate", params), namespace)
    return namespace["result"]


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
