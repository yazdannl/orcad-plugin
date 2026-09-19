"""Gridfinity Baseplate predefined object for orcad.

Standalone module: exposes SPEC (parameter UI) and generate(c) which returns
a self-contained build123d program assigning `result`. No imports allowed
(single-file bundling constraint) — see packaging/bundle.py.

Simplified solid-slab port of kennetek/gridfinity-rebuilt-openscad
gridfinity-rebuilt-baseplate.scad (style_plate=0 "thin" + magnet holes):
slab GX*42 x GY*42, one tapered socket per cell approximating the
baseplate_cutter profile, four 6x2 magnet holes per cell.
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
    gx, gy = c["GX"], c["GY"]
    lines = [
        "from build123d import *",
        f"GX, GY = {gx}, {gy}",
        f"T = {c['T']}",
        # Slab footprint: cells tile at the 42mm pitch (BASEPLATE_DIMENSIONS, gridfinity-baseplate.scad:19).
        "W = GX * 42.0",
        "D = GY * 42.0",
        "result = extrude(Plane.XY * RectangleRounded(W, D, 2.0), amount=T)",
        "for _ix in range(GX):",
        "    for _iy in range(GY):",
        "        _cx = (_ix - (GX - 1) / 2) * 42.0",
        "        _cy = (_iy - (GY - 1) / 2) * 42.0",
    ]
    if c["SOCKETS"]:
        lines += [
            "# socket per cell: tapered pocket approximating baseplate_cutter",
            "# (_BASEPLATE_PROFILE [[0,0],[0.7,0.7],[0.7,2.5],[2.85,4.65]],",
            "# gridfinity-baseplate.scad:38-43; bottom opening ~36.3 wide)",
            "        _sockD = T - 1.2",
            "        _sock = loft(Sketch() + [Pos(_cx, _cy, T - _sockD) * (Plane.XY * RectangleRounded(36.3, 36.3, 1.15)), Pos(_cx, _cy, 0) * (Plane.XY.offset(T + 0.5) * RectangleRounded(40.5, 40.5, 2.5))], ruled=True)",
            "        result -= _sock",
        ]
    if c["MAGNETS"]:
        lines += [
            "# magnet holes: r 3.25, depth 2.4 open at the top (MAGNET_HOLE_DEPTH,",
            "# standard.scad:29), four per cell at 21-8 = 13.0 from center",
            "# (hole_pattern, gridfinity-rebuilt-baseplate.scad:250-256)",
            "        for _dx in (-13.0, 13.0):",
            "            for _dy in (-13.0, 13.0):",
            "                result -= Pos(_cx + _dx, _cy + _dy, T - 2.4) * Cylinder(3.25, 2.9, align=(Align.CENTER, Align.CENTER, Align.MIN))",
        ]
    return "\n".join(lines) + "\n"
