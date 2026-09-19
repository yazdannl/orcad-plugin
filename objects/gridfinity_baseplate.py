"""Gridfinity Baseplate predefined object for orcad.

Standalone module: exposes SPEC (parameter UI) and generate(c) which returns
a self-contained build123d program assigning `result`. No imports allowed
(single-file bundling constraint) — see packaging/bundle.py.
"""

SPEC = {
    "label": "Gridfinity Baseplate",
    "blurb": "Grid the bins snap into, with sockets + magnet holes.",
    "params": [
        {"key": "GX", "label": "Grid X", "unit": "u", "ptype": "int", "min": 1, "max": 6, "step": 1, "default": 4},
        {"key": "GY", "label": "Grid Y", "unit": "u", "ptype": "int", "min": 1, "max": 6, "step": 1, "default": 4},
        {"key": "T", "label": "Thickness", "unit": "mm", "ptype": "number", "min": 4.6, "max": 8, "step": 0.2, "default": 5},
        {"key": "SOCKETS", "label": "Bin sockets", "ptype": "bool", "default": True},
        {"key": "MAGNETS", "label": "Magnet holes (6x2)", "ptype": "bool", "default": True},
    ],
}


def generate(c):
    _pitch, _mag_r = 42.0, 3.25
    gx, gy = c["GX"], c["GY"]
    lines = [
        "from build123d import *",
        f"GX, GY = {gx}, {gy}",
        f"T = {c['T']}",
        f"W = GX * {_pitch}",
        f"D = GY * {_pitch}",
        "result = extrude(Plane.XY * RectangleRounded(W, D, 2.0), amount=T)",
        "for _ix in range(GX):",
        "    for _iy in range(GY):",
        f"        _cx = -W / 2 + {_pitch / 2} + _ix * {_pitch}",
        f"        _cy = -D / 2 + {_pitch / 2} + _iy * {_pitch}",
    ]
    if c["SOCKETS"]:
        lines += [
            "        _sock = Pos(_cx, _cy, T - 2.0) * extrude(Plane.XY * RectangleRounded(40.0, 40.0, 3.0), amount=3.0)",
            "        result -= _sock",
        ]
    if c["MAGNETS"]:
        lines += [
            f"        result -= Pos(_cx, _cy, T - 4.6) * Cylinder({_mag_r}, 2.8, align=(Align.CENTER, Align.CENTER, Align.MIN))",
        ]
    return "\n".join(lines) + "\n"
