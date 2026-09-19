"""Gridfinity Bin predefined object for orcad (Rebuilt-style port).

Standalone module: exposes SPEC (parameter UI) and generate(c) which returns
a self-contained build123d program assigning `result`. No imports allowed
(single-file bundling constraint) — see packaging/bundle.py.

Geometry: spec-based bin (42mm grid, 7mm height units, 0.5 tolerance) with
lofted tapered stacking feet (true 45 deg chamfers), a tapered stacking lip
ring that nests the feet, optional divider walls, front scoop notch and
6x2mm magnet holes on the 26mm-per-cell grid.
"""

SPEC = {
    "label": "Gridfinity Bin",
    "blurb": "Rebuilt-style bin: lofted foot, tapered lip, dividers, scoop, magnets.",
    "params": [
        {"key": "GX", "label": "Grid X", "unit": "u", "ptype": "int", "min": 1, "max": 6, "step": 1, "default": 2},
        {"key": "GY", "label": "Grid Y", "unit": "u", "ptype": "int", "min": 1, "max": 6, "step": 1, "default": 2},
        {"key": "HU", "label": "Height", "unit": "u", "ptype": "int", "min": 1, "max": 12, "step": 1, "default": 6},
        {"key": "WALL", "label": "Wall", "unit": "mm", "ptype": "number", "min": 0.8, "max": 2.4, "step": 0.2, "default": 1.2},
        {"key": "DX", "label": "Dividers X", "unit": "", "ptype": "int", "min": 0, "max": 4, "step": 1, "default": 0},
        {"key": "DY", "label": "Dividers Y", "unit": "", "ptype": "int", "min": 0, "max": 4, "step": 1, "default": 0},
        {"key": "MAGNETS", "label": "Magnet holes (6x2)", "ptype": "bool", "default": True},
        {"key": "LIP", "label": "Stacking lip", "ptype": "bool", "default": True},
        {"key": "SCOOP", "label": "Scoop notch (front)", "ptype": "bool", "default": False},
    ],
}


def generate(c):
    _pitch, _tol, _hu = 42.0, 0.5, 7.0
    _corner, _base_h = 3.75, 4.75
    _mag_r, _mag_d = 3.25, 13.0
    gx, gy, hu = c["GX"], c["GY"], c["HU"]
    lines = [
        "from build123d import *",
        f"GX, GY, HU = {gx}, {gy}, {hu}",
        f"WALL = {c['WALL']}",
        f"BASE_H = {_base_h}",
        f"W = GX * {_pitch} - {_tol}",
        f"D = GY * {_pitch} - {_tol}",
        f"H = HU * {_hu}",
        "# body: rounded walls, open top",
        f"_outer = extrude(Plane.XY * RectangleRounded(W, D, {_corner}), amount=H)",
        "_cw = W - 2 * WALL",
        "_cd = D - 2 * WALL",
        f"_cavity = Pos(0, 0, BASE_H) * extrude(Plane.XY * RectangleRounded(_cw, _cd, max(0.5, {_corner} - WALL)), amount=H - BASE_H + 1)",
        "result = _outer - _cavity",
        "# stacking feet: lofted spec taper (true 45 deg chamfers), one per cell",
        "# profile: (z, width, corner) bottom -> top, mirroring the rebuilt base",
        "_prof = [(0.0, 35.6, 0.8), (0.8, 37.2, 1.5), (2.6, 37.2, 2.5), (4.75, 41.5, 3.75)]",
        "for _ix in range(GX):",
        "    for _iy in range(GY):",
        f"        _cx = -(GX * {_pitch} - {_tol}) / 2 + {_pitch / 2} + _ix * {_pitch}",
        f"        _cy = -(GY * {_pitch} - {_tol}) / 2 + {_pitch / 2} + _iy * {_pitch}",
        "        _secs = [Pos(_cx, _cy, 0) * (Plane.XY.offset(_z) * RectangleRounded(_w, _w, _r)) for _z, _w, _r in _prof]",
        "        result += loft(Sketch() + _secs, ruled=True)",
    ]
    if c["MAGNETS"]:
        lines += [
            "# magnet holes (6x2mm magnets, 26mm grid per cell)",
            "for _ix in range(GX):",
            "    for _iy in range(GY):",
            f"        _cx = -(GX * {_pitch} - {_tol}) / 2 + {_pitch / 2} + _ix * {_pitch}",
            f"        _cy = -(GY * {_pitch} - {_tol}) / 2 + {_pitch / 2} + _iy * {_pitch}",
            f"        for _dx in (-{_mag_d}, {_mag_d}):",
            f"            for _dy in (-{_mag_d}, {_mag_d}):",
            f"                result -= Pos(_cx + _dx, _cy + _dy, -0.5) * Cylinder({_mag_r}, 3.1, align=(Align.CENTER, Align.CENTER, Align.MIN))",
        ]
    if c["LIP"]:
        lines += [
            "# stacking lip: tapered ring above the rim (nests the feet above)",
            "# outer flares past the walls, inner void mirrors the foot taper",
            "_lo0 = W - WALL",
            "_lo1 = W + WALL + 0.6",
            "_li0 = _cw - 0.2",
            "_li1 = _cw + 2 * WALL + 0.4",
            "_lip_outer = loft(Sketch() + [Plane.XY.offset(H) * RectangleRounded(_lo0, _lo0, 3.0), Plane.XY.offset(H + 4.4) * RectangleRounded(_lo1, _lo1, 3.5)], ruled=True)",
            "_lip_inner = loft(Sketch() + [Plane.XY.offset(H - 0.5) * RectangleRounded(_li0, _li0, 2.5), Plane.XY.offset(H + 4.5) * RectangleRounded(_li1, _li1, 3.0)], ruled=True)",
            "result += _lip_outer - _lip_inner",
        ]
    if c["DX"] or c["DY"]:
        lines += ["# divider walls"]
    if c["DX"]:
        lines += [
            f"for _i in range(1, {c['DX']} + 1):",
            f"    _x = -_cw / 2 + _i * _cw / ({c['DX']} + 1)",
            "    result += Pos(_x, 0, BASE_H) * Box(WALL, _cd, H - BASE_H, align=(Align.CENTER, Align.CENTER, Align.MIN))",
        ]
    if c["DY"]:
        lines += [
            f"for _j in range(1, {c['DY']} + 1):",
            f"    _y = -_cd / 2 + _j * _cd / ({c['DY']} + 1)",
            "    result += Pos(0, _y, BASE_H) * Box(_cw, WALL, H - BASE_H, align=(Align.CENTER, Align.CENTER, Align.MIN))",
        ]
    if c["SCOOP"]:
        lines += [
            "# scoop notch in the front wall",
            "_nw = min(_cw * 0.6, _cw - 2 * WALL)",
            "_nh = (H - BASE_H) * 0.45 + 1",
            "_notch = Pos(-_nw / 2, D / 2 - WALL - 1, H + 1 - _nh) * Box(_nw, WALL + 2, _nh, align=(Align.MIN, Align.MIN, Align.MIN))",
            "result -= _notch",
        ]
    return "\n".join(lines) + "\n"
