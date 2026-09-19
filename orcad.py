# /// script
# requires-python = ">=3.12"
# dependencies = ["build123d", "numpy"]
#
# [tool.orcaslicer.plugin]
# name = "orcad"
# description = "build123d CAD tab for OrcaSlicer: searchable parametric objects incl. Gridfinity bins/baseplates, compact Three.js preview, code editor, and STL/STEP/3MF export."
# author = "orcad"
# version = "0.6.0"
# ///
"""orcad — build123d CAD tab (Pages capability).

Top-level "orcad" tab next to Prepare/Preview/Device/Project (same mechanism
as a FilamentHub-style tab): implemented as orca.pages.PagesPluginCapabilityBase.

Layout (v0.5):
- Left: tabbed panel switching between "Objects" (searchable predefined-object
  dropdown + styled parameter sliders, live preview while editing; the Code
  Editor mirrors the selected object + params) and "Code" (a reliable native
  build123d textarea editor).
- Right: persistent Three.js 3D preview (always visible, live for objects) + result + log.
- "Send to plate": exports STL and opens it with the OS default app, so
  OrcaSlicer's single-instance handling loads it onto the build plate
  (one audit prompt on first use, then remembered).
- Predefined objects: Box, Cylinder, Tube, Bracket, Gridfinity Bin,
  Gridfinity Baseplate. Gridfinity geometry follows the public spec
  (42mm grid, 7mm height units, 0.5 tolerance, stacking foot + optional lip
  and magnet holes) with a simplified stepped foot/lip profile.
- Exports go to <plugin_dir>/exports (inside Orca data_dir, no audit prompt).
  Manual import into plater (orca.host is read-only): drag the exported file
  onto the plater.

The UI is inline and framework-free so it works in Orca's embedded WebView.
Three.js is the only page dependency and renders the server tessellation with
native pointer rotation and wheel zoom; the code editor is a plain textarea.

Tested target: OrcaSlicer Nightly / >2.4.2 with `orca.pages` (main branch).
On older builds without orca.pages, falls back to a Script capability that
shows an upgrade message.
"""

import datetime
import json
import math
import os
from contextlib import suppress
from typing import Any, cast
import re
import subprocess
import sys
import threading
import traceback
from pathlib import Path

try:
    import orca  # type: ignore[import-not-found]  # provided by OrcaSlicer embedded interpreter
except ImportError:  # pragma: no cover - allows unit tests without Orca
    orca = None

PLUGIN_VERSION = "0.6.0"
EXPORT_FORMATS = ("stl", "step", "3mf")
DEFAULT_TOLERANCE = 0.001
PREVIEW_MAX_TRIS = 3000  # cap on triangles sent to the page for preview

# Gridfinity spec constants (public spec: 42mm grid, 7mm height units).
GF_PITCH = 42.0
GF_TOL = 0.5          # total clearance -> bin outer = units*42 - 0.5
GF_HU = 7.0           # height unit
GF_CORNER = 3.75      # bin corner radius
GF_BASE_H = 4.75      # foot height (part of total height)
GF_MAG_R = 3.25       # magnet hole radius (6.5 dia for 6x2 magnets)
GF_MAG_D = 13.0       # magnet hole offset from cell center (26mm square)

# ---------------------------------------------------------------------------
# Pure logic (no orca / build123d import required — unit-testable)
# Param spec: {"key","label","ptype" ("number"|"int"|"bool"),
#              "min","max","step","default","hint"}
# ---------------------------------------------------------------------------


def _num(v, name):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {v!r}") from None
    if not math.isfinite(f):
        raise ValueError(f"{name} must be finite")
    return f


def _bool(v, name):
    if isinstance(v, bool):
        return v
    if isinstance(v, (int, float)) and v in (0, 1):
        return bool(v)
    if isinstance(v, str) and v.strip().lower() in ("true", "1", "on", "yes"):
        return True
    if isinstance(v, str) and v.strip().lower() in ("false", "0", "off", "no"):
        return False
    raise ValueError(f"{name} must be true/false, got {v!r}")


def validate_primitive_params(primitive, params):
    """Raise ValueError on bad params; return cleaned dict."""
    if primitive not in PRIMITIVES:
        raise ValueError(f"unknown object {primitive!r}")
    cleaned = {}
    for p in PRIMITIVES[primitive]["params"]:
        key = p["key"]
        ptype = p.get("ptype", "number")
        if key not in params:
            raise ValueError(f"missing param {key}")
        raw = params[key]
        if ptype == "bool":
            cleaned[key] = _bool(raw, key)
            continue
        v = _num(raw, key)
        if v < p["min"] or v > p["max"]:
            raise ValueError(f"{key}={v} out of range [{p['min']}, {p['max']}]")
        if ptype == "int":
            if not v.is_integer():
                raise ValueError(f"{key} must be a whole number")
            try:
                cleaned[key] = int(v)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError(f"{key} must be a whole number") from exc
        else:
            cleaned[key] = v
    if primitive == "tube" and not cleaned["R_IN"] < cleaned["R_OUT"]:
        raise ValueError("R_IN must be smaller than R_OUT")
    if primitive == "bracket" and cleaned["D"] >= min(cleaned["L"] / 2, cleaned["W"]):
            raise ValueError("Hole diameter D too large for plate size")
    if primitive == "gridfinity_bin" and cleaned["REFINED"] and cleaned["MAGNETS"]:
            raise ValueError("refined holes exclude magnet holes (original rule)")
    if primitive == "gridfinity_baseplate":
        if cleaned["REFINED"] and cleaned["MAGNETS"]:
            raise ValueError("refined holes exclude magnet holes (original rule)")
        if cleaned["MAGNETS"] and cleaned["T"] < 4.6:
            raise ValueError("Thickness >= 4.6mm needed for magnet holes")
    return cleaned


_SPEC_LINE_RE = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_]*)\s*=\s*[^#\n]*#\s*spec\s*:.*$"
)


def _bake_template(template, values):
    """Bake validated params into `# spec:` lines of an object program."""
    out = []
    for line in template.splitlines():
        match = _SPEC_LINE_RE.match(line)
        if match and match.group(1) in values:
            comment = "#" + line.split("#", 1)[1]
            line = f"{match.group(1)} = {values[match.group(1)]!r}  {comment}"
        out.append(line)
    return "\n".join(out) + "\n"


# BEGIN BUNDLED OBJECTS
# Generated by `python3 packaging/bundle.py --write`.
# Do not edit here; edit objects/*.py instead.
box_SPEC = {'label': 'Box', 'blurb': 'Simple centered block.', 'params': [{'key': 'L', 'label': 'Length', 'unit': 'mm', 'ptype': 'number', 'default': 20, 'min': 1.0, 'max': 300.0, 'step': 0.5}, {'key': 'W', 'label': 'Width', 'unit': 'mm', 'ptype': 'number', 'default': 20, 'min': 1.0, 'max': 300.0, 'step': 0.5}, {'key': 'H', 'label': 'Height', 'unit': 'mm', 'ptype': 'number', 'default': 20, 'min': 1.0, 'max': 300.0, 'step': 0.5}]}


box_TEMPLATE = r"""
# object: Box
# blurb: Simple centered block.
# Box for orcad — runnable build123d program (see header above).
from build123d import *

L = 20  # spec: number label=Length unit=mm min=1 max=300 step=0.5
W = 20  # spec: number label=Width unit=mm min=1 max=300 step=0.5
H = 20  # spec: number label=Height unit=mm min=1 max=300 step=0.5

result = Box(L, W, H)
"""


bracket_SPEC = {'label': 'Bracket plate', 'blurb': 'Flat plate with two holes.', 'params': [{'key': 'L', 'label': 'Length', 'unit': 'mm', 'ptype': 'number', 'default': 60, 'min': 10.0, 'max': 300.0, 'step': 0.5}, {'key': 'W', 'label': 'Width', 'unit': 'mm', 'ptype': 'number', 'default': 30, 'min': 10.0, 'max': 200.0, 'step': 0.5}, {'key': 'T', 'label': 'Thickness', 'unit': 'mm', 'ptype': 'number', 'default': 5, 'min': 1.0, 'max': 50.0, 'step': 0.5}, {'key': 'D', 'label': 'Hole dia', 'unit': 'mm', 'ptype': 'number', 'default': 5, 'min': 1.0, 'max': 50.0, 'step': 0.5}]}


bracket_TEMPLATE = r"""
# object: Bracket plate
# blurb: Flat plate with two holes.
# Bracket plate for orcad — runnable build123d program (see header above).
from build123d import *

L = 60  # spec: number label=Length unit=mm min=10 max=300 step=0.5
W = 30  # spec: number label=Width unit=mm min=10 max=200 step=0.5
T = 5  # spec: number label=Thickness unit=mm min=1 max=50 step=0.5
D = 5  # spec: number label=Hole dia unit=mm min=1 max=50 step=0.5

plate = Box(L, W, T)
hole = Cylinder(D / 2, T + 2)
h1 = Pos(-L / 4, 0, -1) * hole
h2 = Pos(L / 4, 0, -1) * hole
result = plate - h1 - h2
"""


cylinder_SPEC = {'label': 'Cylinder', 'blurb': 'Round post or puck.', 'params': [{'key': 'R', 'label': 'Radius', 'unit': 'mm', 'ptype': 'number', 'default': 10, 'min': 0.5, 'max': 150.0, 'step': 0.5}, {'key': 'H', 'label': 'Height', 'unit': 'mm', 'ptype': 'number', 'default': 20, 'min': 1.0, 'max': 300.0, 'step': 0.5}]}


cylinder_TEMPLATE = r"""
# object: Cylinder
# blurb: Round post or puck.
# Cylinder for orcad — runnable build123d program (see header above).
from build123d import *

R = 10  # spec: number label=Radius unit=mm min=0.5 max=150 step=0.5
H = 20  # spec: number label=Height unit=mm min=1 max=300 step=0.5

result = Cylinder(R, H)
"""


gridfinity_baseplate_SPEC = {'label': 'Gridfinity Baseplate', 'blurb': 'Grid the bins snap into, with sockets + magnet holes.', 'params': [{'key': 'GX', 'label': 'Grid X', 'unit': 'u', 'ptype': 'int', 'default': 4, 'min': 1.0, 'max': 6.0, 'step': 1.0}, {'key': 'GY', 'label': 'Grid Y', 'unit': 'u', 'ptype': 'int', 'default': 4, 'min': 1.0, 'max': 6.0, 'step': 1.0}, {'key': 'T', 'label': 'Thickness', 'unit': 'mm', 'ptype': 'number', 'default': 5, 'min': 4.6, 'max': 8.0, 'step': 0.2}, {'key': 'STYLE', 'label': 'Plate style', 'unit': '', 'ptype': 'int', 'default': 0, 'min': 0.0, 'max': 4.0, 'step': 1.0}, {'key': 'HOLESTYLE', 'label': 'Mount holes', 'unit': '', 'ptype': 'int', 'default': 0, 'min': 0.0, 'max': 2.0, 'step': 1.0}, {'key': 'DISTX', 'label': 'Minimum X', 'unit': 'mm', 'ptype': 'number', 'default': 0, 'min': 0.0, 'max': 300.0, 'step': 1.0}, {'key': 'DISTY', 'label': 'Minimum Y', 'unit': 'mm', 'ptype': 'number', 'default': 0, 'min': 0.0, 'max': 300.0, 'step': 1.0}, {'key': 'FITX', 'label': 'Fit X', 'unit': '', 'ptype': 'number', 'default': 0, 'min': -1.0, 'max': 1.0, 'step': 0.1}, {'key': 'FITY', 'label': 'Fit Y', 'unit': '', 'ptype': 'number', 'default': 0, 'min': -1.0, 'max': 1.0, 'step': 0.1}, {'key': 'SCREW_D', 'label': 'Screw diameter', 'unit': 'mm', 'ptype': 'number', 'default': 3.35, 'min': 2.0, 'max': 5.0, 'step': 0.05}, {'key': 'SCREW_HEAD', 'label': 'Screw head diameter', 'unit': 'mm', 'ptype': 'number', 'default': 5, 'min': 3.0, 'max': 8.0, 'step': 0.1}, {'key': 'SCREW_SPACING', 'label': 'Screw spacing', 'unit': 'mm', 'ptype': 'number', 'default': 0.5, 'min': 0.0, 'max': 2.0, 'step': 0.1}, {'key': 'NSCREWS', 'label': 'Screws per seam', 'unit': '', 'ptype': 'int', 'default': 1, 'min': 1.0, 'max': 3.0, 'step': 1.0}, {'key': 'SOCKETS', 'label': 'Bin sockets', 'unit': '', 'ptype': 'bool', 'default': True}, {'key': 'REFINED', 'label': 'Refined holes', 'unit': '', 'ptype': 'bool', 'default': False}, {'key': 'MAGNETS', 'label': 'Magnet holes (6x2)', 'unit': '', 'ptype': 'bool', 'default': True}, {'key': 'SCREW', 'label': 'Screw holes (M3)', 'unit': '', 'ptype': 'bool', 'default': False}, {'key': 'CRUSH', 'label': 'Crush ribs', 'unit': '', 'ptype': 'bool', 'default': True}, {'key': 'CHAMFER', 'label': 'Hole chamfer', 'unit': '', 'ptype': 'bool', 'default': True}, {'key': 'PRINTABLE', 'label': 'Supportless hole tops', 'unit': '', 'ptype': 'bool', 'default': False}, {'key': 'CORNERS', 'label': 'Holes only at corners', 'unit': '', 'ptype': 'bool', 'default': False}]}


gridfinity_baseplate_TEMPLATE = r"""
# object: Gridfinity Baseplate
# blurb: Grid the bins snap into, with sockets + magnet holes.
# Thin-style slab port of kennetek/gridfinity-rebuilt-openscad
# gridfinity-rebuilt-baseplate.scad: slab GX*42 x GY*42, one tapered socket
# per cell, four 6x2 magnet holes per cell. Hole shapes mirror the bin
# builder (block_base_hole), flipped to open at the top surface.
# Full plate styles (weighted/skeletonized/screw-together/fit-to-drawer)
# are roadmap, not here.
# Parameter variables carry `# spec:` comments; packaging/bundle.py extracts
# the UI spec from them. Run standalone with build123d installed, or use
# through the orcad tab.
from build123d import *
import math

GX = 4  # spec: int label=Grid X unit=u min=1 max=6 step=1
GY = 4  # spec: int label=Grid Y unit=u min=1 max=6 step=1
T = 5  # spec: number label=Thickness unit=mm min=4.6 max=8 step=0.2
STYLE = 0  # spec: int label=Plate style min=0 max=4 step=1
HOLESTYLE = 0  # spec: int label=Mount holes min=0 max=2 step=1
DISTX = 0  # spec: number label=Minimum X unit=mm min=0 max=300 step=1
DISTY = 0  # spec: number label=Minimum Y unit=mm min=0 max=300 step=1
FITX = 0  # spec: number label=Fit X min=-1 max=1 step=0.1
FITY = 0  # spec: number label=Fit Y min=-1 max=1 step=0.1
SCREW_D = 3.35  # spec: number label=Screw diameter unit=mm min=2 max=5 step=0.05
SCREW_HEAD = 5  # spec: number label=Screw head diameter unit=mm min=3 max=8 step=0.1
SCREW_SPACING = 0.5  # spec: number label=Screw spacing unit=mm min=0 max=2 step=0.1
NSCREWS = 1  # spec: int label=Screws per seam min=1 max=3 step=1
SOCKETS = True  # spec: bool label=Bin sockets
REFINED = False  # spec: bool label=Refined holes
MAGNETS = True  # spec: bool label=Magnet holes (6x2)
SCREW = False  # spec: bool label=Screw holes (M3)
CRUSH = True  # spec: bool label=Crush ribs
CHAMFER = True  # spec: bool label=Hole chamfer
PRINTABLE = False  # spec: bool label=Supportless hole tops
CORNERS = False  # spec: bool label=Holes only at corners

# Slab footprint: cells tile at the 42mm pitch (BASEPLATE_DIMENSIONS, gridfinity-baseplate.scad:19).
_W0, _D0 = GX * 42.0, GY * 42.0
W, D = max(_W0, DISTX), max(_D0, DISTY)
_PX = (W - _W0) * (FITX / 2 + 0.5)
_PY = (D - _D0) * (FITY / 2 + 0.5)
_EXTRA = 6.4 if STYLE == 1 else (1.0 if STYLE == 2 else (6.75 if STYLE in (3, 4) else 0.0))
_PT = T + _EXTRA
result = extrude(Plane.XY * RectangleRounded(W, D, 2.0), amount=_PT)
for _ix in range(GX):
    for _iy in range(GY):
        _cx = (_ix - (GX - 1) / 2) * 42.0 + _PX
        _cy = (_iy - (GY - 1) / 2) * 42.0 + _PY
        if SOCKETS:
            # socket per cell: tapered pocket approximating baseplate_cutter
            # (_BASEPLATE_PROFILE [[0,0],[0.7,0.7],[0.7,2.5],[2.85,4.65]],
            # gridfinity-baseplate.scad:38-43; bottom opening ~36.3 wide)
            _sockD = _PT - 1.2
            _sock = loft(Sketch() + [Pos(_cx, _cy, _PT - _sockD) * (Plane.XY * RectangleRounded(36.3, 36.3, 1.15)), Pos(_cx, _cy, 0) * (Plane.XY.offset(_PT + 0.5) * RectangleRounded(40.5, 40.5, 2.5))], ruled=True)
            result -= _sock
        if STYLE == 1:
            # weighted style: four underside weight pockets per grid cell
            for _sx in (-10.7, 10.7):
                for _sy in (-10.7, 10.7):
                    result -= Pos(_cx + _sx, _cy + _sy, -0.01) * Box(15.5, 15.5, 4.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
        elif STYLE in (2, 4):
            # skeletonized/minimal styles remove the broad underside web while
            # retaining the perimeter and the socket walls.
            result -= Pos(_cx, _cy, -0.01) * Box(21.4, 21.4, max(1.0, _PT - 1.0), align=(Align.CENTER, Align.CENTER, Align.MIN))
# ---- holes open at the top surface (mirrored block_base_hole) ----
def _plate_hole_xy():
    if CORNERS:
        _hx = (W - 5.9) / 2 - 4.8
        _hy = (D - 5.9) / 2 - 4.8
        return [(-_hx, -_hy), (-_hx, _hy), (_hx, -_hy), (_hx, _hy)]
    _pos = []
    for _ix in range(GX):
        for _iy in range(GY):
            _cx = (_ix - (GX - 1) / 2) * 42.0 + _PX
            _cy = (_iy - (GY - 1) / 2) * 42.0 + _PY
            for _dx in (-13.0, 13.0):
                for _dy in (-13.0, 13.0):
                    _pos.append((_cx + _dx, _cy + _dy))
    return _pos
def _plate_printable(_inner, _outer):
    # stepped bridging ceiling just under the top surface (mirrored
    # make_hole_printable); local frame spans the top 0.84 of the hole
    _od = 2 * (_outer + 0.02)
    _id = 2 * (_inner + 0.02)
    _per = (_od - _id) / 2
    _solid = Pos(-(_od + 0.02) / 2, -(_od + 0.02) / 2, 1.68) * Box(_od + 0.02, _od + 0.02, 0.84, align=(Align.MIN, Align.MIN, Align.MIN))
    for _w1, _w2, _zz, _rt in ((_od, _od - _per, 1.66, False), (_od - _per, _od - 2 * _per, 1.86, True), (_od - 2 * _per, _od - 2 * _per, 2.06, False)):
        _a, _b = (_w2, _w1) if _rt else (_w1, _w2)
        _solid -= Pos(-_a / 2, -_b / 2, _zz) * Box(_a, _b, 0.24, align=(Align.MIN, Align.MIN, Align.MIN))
    return _solid
if REFINED or MAGNETS or SCREW:
    for (_hx, _hy) in _plate_hole_xy():
        if REFINED:
            # refined hole, single orientation (hole_pattern has no rotation)
            _ref = Pos(0, -2.93, -1.9) * Box(11, 5.86, 1.9, align=(Align.MIN, Align.MIN, Align.MIN))
            _ref += Pos(0, 0, -1.9) * Cylinder(2.93, 1.9, align=(Align.CENTER, Align.CENTER, Align.MIN))
            _ref += Pos(-6.93, -1.25, -2.5) * Box(4.4, 2.5, 2.5, align=(Align.MIN, Align.MIN, Align.MIN))
            _ref += Pos(-6.93, 0, -2.5) * Cylinder(1.25, 2.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
            result -= Pos(_hx, _hy, _PT) * _ref
        if MAGNETS:
            if CRUSH:
                _pts = []
                for _i in range(64):
                    _a = _i * 360.0 / 64
                    _r = 3.1 + 0.15 * math.sin(math.radians(_a * 8))
                    _pts.append((_r * math.sin(math.radians(_a)), _r * math.cos(math.radians(_a))))
                _pts.append(_pts[0])
                _mhole = extrude(Plane.XY * make_face(Polyline(*_pts)), amount=2.4)
            else:
                _mhole = Cylinder(3.25, 2.4, align=(Align.CENTER, Align.CENTER, Align.MIN))
            if PRINTABLE:
                _mhole -= _plate_printable(1.5 if SCREW else 1.0, 3.25)
            if CHAMFER:
                _mhole += Cone(1.65, 4.05, 2.4, align=(Align.CENTER, Align.CENTER, Align.MIN))
            result -= Pos(_hx, _hy, _PT - 2.4) * _mhole
        if SCREW:
            _shole = Pos(0, 0, -0.25) * Cylinder(SCREW_D / 2, _PT + 0.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
            if PRINTABLE:
                _shole -= Pos(0, 0, _PT - 2.4) * _plate_printable(0.5, SCREW_D / 2)
            if CHAMFER or HOLESTYLE == 1:
                _shole += Pos(0, 0, _PT - 0.8) * Cone(SCREW_D / 2, SCREW_HEAD / 2, 0.8, align=(Align.CENTER, Align.CENTER, Align.MIN))
            elif HOLESTYLE == 2:
                _shole += Pos(0, 0, _PT - 2.0) * Cylinder(SCREW_HEAD / 2, 2.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
            result -= Pos(_hx, _hy, 0) * _shole

# Screw-together plates use horizontal clearance tunnels at the outside seams.
# This is the same user-visible feature as the upstream style 3/4 cutter,
# expressed as simple cylinders so it remains robust in build123d.
if STYLE in (3, 4):
    _r = SCREW_D / 2
    for _x in (-W / 2, W / 2):
        for _i in range(max(1, NSCREWS)):
            _y = (_i - (max(1, NSCREWS) - 1) / 2) * (SCREW_HEAD + SCREW_SPACING)
            result -= Pos(_x, _y, _PT / 2) * Rot(0, 90, 0) * Cylinder(_r, 4.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    for _y in (-D / 2, D / 2):
        for _i in range(max(1, NSCREWS)):
            _x = (_i - (max(1, NSCREWS) - 1) / 2) * (SCREW_HEAD + SCREW_SPACING)
            result -= Pos(_x, _y, _PT / 2) * Rot(90, 0, 0) * Cylinder(_r, 4.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
"""


gridfinity_bin_SPEC = {'label': 'Gridfinity Bin', 'blurb': 'Full Rebuilt port: compartments, tabs, scoop, holes, lip, height modes.', 'params': [{'key': 'GX', 'label': 'Grid X', 'unit': 'u', 'ptype': 'int', 'default': 2, 'min': 1.0, 'max': 6.0, 'step': 1.0}, {'key': 'GY', 'label': 'Grid Y', 'unit': 'u', 'ptype': 'int', 'default': 2, 'min': 1.0, 'max': 6.0, 'step': 1.0}, {'key': 'HU', 'label': 'Height value', 'unit': '', 'ptype': 'int', 'default': 6, 'min': 0.0, 'max': 200.0, 'step': 1.0}, {'key': 'HMODE', 'label': 'Height mode 0U 1in 2ex 3exlip', 'unit': '', 'ptype': 'int', 'default': 0, 'min': 0.0, 'max': 3.0, 'step': 1.0}, {'key': 'ZS', 'label': 'Snap height to 7mm', 'unit': '', 'ptype': 'bool', 'default': False}, {'key': 'FILL', 'label': 'Solid fill mm (0=auto)', 'unit': 'mm', 'ptype': 'number', 'default': 0, 'min': 0.0, 'max': 200.0, 'step': 1.0}, {'key': 'WALL', 'label': 'Outer wall', 'unit': 'mm', 'ptype': 'number', 'default': 0.95, 'min': 0.95, 'max': 2.4, 'step': 0.05}, {'key': 'DX', 'label': 'Divisions X (0=solid)', 'unit': '', 'ptype': 'int', 'default': 1, 'min': 0.0, 'max': 6.0, 'step': 1.0}, {'key': 'DY', 'label': 'Divisions Y (0=solid)', 'unit': '', 'ptype': 'int', 'default': 1, 'min': 0.0, 'max': 6.0, 'step': 1.0}, {'key': 'DEPTH', 'label': 'Compartment depth mm (0=full)', 'unit': 'mm', 'ptype': 'number', 'default': 0, 'min': 0.0, 'max': 200.0, 'step': 1.0}, {'key': 'SCOOPW', 'label': 'Scoop amount', 'unit': '', 'ptype': 'number', 'default': 1.0, 'min': 0.0, 'max': 1.0, 'step': 0.1}, {'key': 'TABSTYLE', 'label': 'Tab 0Full 1Auto 2Left 3Center 4Right 5None', 'unit': '', 'ptype': 'int', 'default': 1, 'min': 0.0, 'max': 5.0, 'step': 1.0}, {'key': 'TABPLACE', 'label': 'Tabs only top-left', 'unit': '', 'ptype': 'int', 'default': 0, 'min': 0.0, 'max': 1.0, 'step': 1.0}, {'key': 'CYL', 'label': 'Cylindrical compartments', 'unit': '', 'ptype': 'bool', 'default': False}, {'key': 'CD', 'label': 'Cylinder dia', 'unit': 'mm', 'ptype': 'number', 'default': 10, 'min': 1.0, 'max': 60.0, 'step': 0.5}, {'key': 'CCHAM', 'label': 'Cylinder top chamfer', 'unit': 'mm', 'ptype': 'number', 'default': 0.5, 'min': 0.0, 'max': 5.0, 'step': 0.1}, {'key': 'REFINED', 'label': 'Refined holes', 'unit': '', 'ptype': 'bool', 'default': True}, {'key': 'MAGNETS', 'label': 'Magnet holes (6x2)', 'unit': '', 'ptype': 'bool', 'default': False}, {'key': 'SCREW', 'label': 'Screw holes (M3)', 'unit': '', 'ptype': 'bool', 'default': False}, {'key': 'CRUSH', 'label': 'Crush ribs', 'unit': '', 'ptype': 'bool', 'default': True}, {'key': 'CHAMFER', 'label': 'Hole chamfer', 'unit': '', 'ptype': 'bool', 'default': True}, {'key': 'PRINTABLE', 'label': 'Supportless hole tops', 'unit': '', 'ptype': 'bool', 'default': True}, {'key': 'CORNERS', 'label': 'Holes only at corners', 'unit': '', 'ptype': 'bool', 'default': False}, {'key': 'THUMB', 'label': 'Thumbscrew holes', 'unit': '', 'ptype': 'bool', 'default': False}, {'key': 'LIP', 'label': 'Stacking lip', 'unit': '', 'ptype': 'bool', 'default': True}]}


gridfinity_bin_TEMPLATE = r"""
# object: Gridfinity Bin
# blurb: Full Rebuilt port: compartments, tabs, scoop, holes, lip, height modes.
# Faithful port of kennetek/gridfinity-rebuilt-openscad
# (gridfinity-rebuilt-bins.scad). Construction mirrors the original CSG tree:
# tapered feet + bridge + base holes, wall ring, infill solid, per-compartment
# rounded cutters (minus scoop/tab solids) or cylinders, stacking lip ring.
# Parameter variables carry `# spec:` comments; packaging/bundle.py extracts
# the UI spec from them. Run standalone with build123d installed, or use
# through the orcad tab.
from build123d import *
import math

GX = 2  # spec: int label=Grid X unit=u min=1 max=6 step=1
GY = 2  # spec: int label=Grid Y unit=u min=1 max=6 step=1
HU = 6  # spec: int label=Height value min=0 max=200 step=1
HMODE = 0  # spec: int label=Height mode 0U 1in 2ex 3exlip min=0 max=3 step=1
ZS = False  # spec: bool label=Snap height to 7mm
FILL = 0  # spec: number label=Solid fill mm (0=auto) unit=mm min=0 max=200 step=1
WALL = 0.95  # spec: number label=Outer wall unit=mm min=0.95 max=2.4 step=0.05
DX = 1  # spec: int label=Divisions X (0=solid) min=0 max=6 step=1
DY = 1  # spec: int label=Divisions Y (0=solid) min=0 max=6 step=1
DEPTH = 0  # spec: number label=Compartment depth mm (0=full) unit=mm min=0 max=200 step=1
SCOOPW = 1.0  # spec: number label=Scoop amount min=0 max=1 step=0.1
TABSTYLE = 1  # spec: int label=Tab 0Full 1Auto 2Left 3Center 4Right 5None min=0 max=5 step=1
TABPLACE = 0  # spec: int label=Tabs only top-left min=0 max=1 step=1
CYL = False  # spec: bool label=Cylindrical compartments
CD = 10  # spec: number label=Cylinder dia unit=mm min=1 max=60 step=0.5
CCHAM = 0.5  # spec: number label=Cylinder top chamfer unit=mm min=0 max=5 step=0.1
REFINED = True  # spec: bool label=Refined holes
MAGNETS = False  # spec: bool label=Magnet holes (6x2)
SCREW = False  # spec: bool label=Screw holes (M3)
CRUSH = True  # spec: bool label=Crush ribs
CHAMFER = True  # spec: bool label=Hole chamfer
PRINTABLE = True  # spec: bool label=Supportless hole tops
CORNERS = False  # spec: bool label=Holes only at corners
THUMB = False  # spec: bool label=Thumbscrew holes
LIP = True  # spec: bool label=Stacking lip

# ---- height (gridfinity-rebuilt-utility.scad: height() + z_snap) ----
_Hraw = HU * 7.0 if HMODE == 0 else (HU + 7.0 if HMODE == 1 else (HU if HMODE == 2 else HU - 4.4))
if ZS:
    _Hraw = _Hraw if _Hraw % 7 == 0 else _Hraw + 7 - _Hraw % 7
H = max(_Hraw, 7.0)
assert H >= 7.0, "height below 7mm base"
assert not LIP or FILL <= 0 or FILL <= H - 1.2, "fill too tall for lipped bin"
W = GX * 42.0 - 0.5
D = GY * 42.0 - 0.5
_EW = max(WALL, 0.95)
_lip_sup = 1.2 if LIP else 0.0
_fill = FILL if FILL > 0 else H - 7.0 - _lip_sup
_infill_top = 7.0 + _fill
# ---- tapered stacking feet, one per cell (lofted spec profile) ----
_feet = None
_prof = [(0.0, 35.6, 0.8), (0.8, 37.2, 0.8), (2.6, 37.2, 0.8), (4.75, 41.5, 3.75)]
for _ix in range(GX):
    for _iy in range(GY):
        _cx = (_ix - (GX - 1) / 2) * 42.0
        _cy = (_iy - (GY - 1) / 2) * 42.0
        _secs = [Pos(_cx, _cy, 0) * (Plane.XY.offset(_z) * RectangleRounded(_w, _w, _r)) for _z, _w, _r in _prof]
        _foot = loft(Sketch() + _secs, ruled=True)
        _feet = _foot if _feet is None else _feet + _foot
result = _feet
# ---- bridge slab tying the feet together ----
result += Pos(0, 0, 4.75) * extrude(Plane.XY * RectangleRounded(W, D, 3.75), amount=2.25)
# ---- base holes (magnet/screw/refined options per cell or outer corners) ----
def _hole_positions():
    if CORNERS:
        _hx = (W - 5.9) / 2 - 4.8
        _hy = (D - 5.9) / 2 - 4.8
        return [(-_hx, -_hy), (-_hx, _hy), (_hx, -_hy), (_hx, _hy)]
    _pos = []
    for _ix in range(GX):
        for _iy in range(GY):
            _cx = (_ix - (GX - 1) / 2) * 42.0
            _cy = (_iy - (GY - 1) / 2) * 42.0
            for _dx in (-13.0, 13.0):
                for _dy in (-13.0, 13.0):
                    _pos.append((_cx + _dx, _cy + _dy))
    return _pos
def _printable_steps(_inner, _outer, _h):
    # stepped bridging ceiling, literal port of make_hole_printable (3 layers)
    _od = 2 * (_outer + 0.02)
    _id = 2 * (_inner + 0.02)
    _per = (_od - _id) / 2
    _adj = _h - 0.6
    _solid = Pos(-(_od + 0.02) / 2, -(_od + 0.02) / 2, _adj) * Box(_od + 0.02, _od + 0.02, 0.72, align=(Align.MIN, Align.MIN, Align.MIN))
    for _k, _w1, _w2, _zz, _rt in ((1, _od, _od - _per, _adj - 0.02, False), (2, _od - _per, _od - 2 * _per, _adj + 0.18, True), (3, _od - 2 * _per, _od - 2 * _per, _adj + 0.38, False)):
        _a, _b = (_w2, _w1) if _rt else (_w1, _w2)
        _solid -= Pos(-_a / 2, -_b / 2, _zz) * Box(_a, _b, 0.24, align=(Align.MIN, Align.MIN, Align.MIN))
    return _solid
if REFINED or MAGNETS or SCREW or THUMB:
    _positions = _hole_positions()
    if REFINED:
        # refined hole: side-entry slot + poke hole, rotated per quadrant
        _ref = Pos(0, -2.93, 0.4) * Box(11, 5.86, 1.9, align=(Align.MIN, Align.MIN, Align.MIN))
        _ref += Pos(0, 0, 0.4) * Cylinder(2.93, 1.9, align=(Align.CENTER, Align.CENTER, Align.MIN))
        _ref += Pos(-6.93, -1.25, -0.2) * Box(4.4, 2.5, 2.5, align=(Align.MIN, Align.MIN, Align.MIN))
        _ref += Pos(-6.93, 0, -0.2) * Cylinder(1.25, 2.5, align=(Align.CENTER, Align.CENTER, Align.MIN))
        for (_qx, _qy, _rot) in ((1, 1, 0), (-1, 1, 90), (-1, -1, 180), (1, -1, 270)):
            for (_hx, _hy) in _positions:
                if (_hx > 0) == (_qx > 0) and (_hy > 0) == (_qy > 0):
                    result -= Pos(_hx, _hy, 0) * Rot(0, 0, _rot) * _ref
    if MAGNETS:
        _mdepth = 2.4 + (0.6 if PRINTABLE else 0.0)
        if CRUSH:
            _pts = []
            for _i in range(64):
                _a = _i * 360.0 / 64
                _r = 3.1 + 0.15 * math.sin(math.radians(_a * 8))
                _pts.append((_r * math.sin(math.radians(_a)), _r * math.cos(math.radians(_a))))
            _pts.append(_pts[0])
            _mhole = extrude(Plane.XY * make_face(Polyline(*_pts)), amount=_mdepth)
        else:
            _mhole = Cylinder(3.25, _mdepth, align=(Align.CENTER, Align.CENTER, Align.MIN))
        if PRINTABLE:
            _mhole -= _printable_steps(1.5 if SCREW else 1.0, 3.25, _mdepth)
        if CHAMFER:
            _mhole += Cone(4.05, max(0.05, 4.05 - 2.4), 2.4, align=(Align.CENTER, Align.CENTER, Align.MIN))
        for (_hx, _hy) in _positions:
            result -= Pos(_hx, _hy, 0) * _mhole
    if SCREW:
        _shole = Cylinder(1.5, 7.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
        if PRINTABLE:
            _shole -= _printable_steps(0.5, 1.5, 7.0)
        if CHAMFER:
            _shole += Cone(2.3, 0, 2.3, align=(Align.CENTER, Align.CENTER, Align.MIN))
        for (_hx, _hy) in _positions:
            result -= Pos(_hx, _hy, 0) * _shole
    if THUMB:
        for (_tx, _ty) in ([((_ix - (GX - 1) / 2) * 42.0, (_iy - (GY - 1) / 2) * 42.0) for _ix in range(GX) for _iy in range(GY)] if not CORNERS else [(_hx / 2, _hy / 2) for _hx in (-(W - 41.5) / 2, (W - 41.5) / 2) for _hy in (-(D - 41.5) / 2, (D - 41.5) / 2)]):
            result -= Pos(_tx, _ty, 0) * Cylinder(7.8, 4.75, align=(Align.CENTER, Align.CENTER, Align.MIN))
# ---- walls: thin ring + infill solid ----
if H > 7.0:
    _wall = extrude(Plane.XY * RectangleRounded(W, D, 3.75), amount=H - 7.0) - Pos(0, 0, -0.5) * extrude(Plane.XY * RectangleRounded(W - 2 * _EW, D - 2 * _EW, 3.75), amount=H - 7.0 + 1)
    result += Pos(0, 0, 7.0) * _wall
if _fill > 0:
    result += Pos(0, 0, 7.0) * extrude(Plane.XY * RectangleRounded(W - 0.5, D - 0.5, 3.75), amount=_fill)
# ---- compartments: per-division rounded cutters (element minus 0.6 total),
# minus scoop/tab solids; cylinders replace cutters when CYL ----
if DX > 0 and DY > 0 and _fill > 0:
    # compartment grid spans the spec infill (total minus 2x0.95 walls),
    # independent of our outer-wall setting; cutters inset 0.3 per side
    _rx = (W - 1.9) / DX
    _ry = (D - 1.9) / DY
    _ztop = _infill_top + 0.02
    _dep = DEPTH if DEPTH > 0 else _fill
    for _ix in range(DX):
        for _iy in range(DY):
            _cx = -(W - 1.9) / 2 + (_ix + 0.5) * _rx
            _cy = -(D - 1.9) / 2 + (_iy + 0.5) * _ry
            if CYL:
                _ccut = Cylinder(CD / 2, _dep + 0.02, align=(Align.CENTER, Align.CENTER, Align.MIN))
                if CCHAM > 0:
                    _ccut += Pos(0, 0, _dep + 0.02 - CCHAM) * Cone(CD / 2, CD / 2 + CCHAM, CCHAM, align=(Align.CENTER, Align.CENTER, Align.MIN))
                result -= Pos(_cx, _cy, _ztop - _dep) * _ccut
                continue
            _cw = _rx - 0.6
            _cd = _ry - 0.6
            _ch = _dep + 0.02
            _cr = min(2.8, _cw / 2 - 0.01, _cd / 2 - 0.01, _ch / 2 - 0.01)
            _cut = Box(_cw, _cd, _ch)
            if _cr > 0.5:
                _cut = fillet(_cut.edges(), _cr)
            _cut = Pos(0, 0, -_ch / 2) * _cut
            if SCOOPW > 0:
                # finger ramp at the -y wall: box minus x-axis cylinder
                _s = SCOOPW * _dep / 2
                if _s > 0.1:
                    _scoop = Pos(-_cw / 2, -_cd / 2, -_ch) * Box(_cw, _s, _s, align=(Align.MIN, Align.MIN, Align.MIN)) - Pos(0, -_cd / 2 + _s, -_ch + _s) * Rot(0, 90, 0) * Cylinder(_s, _cw + 2)
                    _cut -= _scoop
            # NOTE: the original only documents tab auto-disable below 3U but does
            # not enforce it; tabs render at any height exactly like here.
            _tabbed = TABSTYLE != 5 and (not TABPLACE or (_ix == 0 and _iy == DY - 1))
            if _tabbed:
                # label wedge on the +y wall: exact TAB_POLYGON profile
                _tw = max(_cw, _cd, _dep) if TABSTYLE == 0 else 42.0
                if TABSTYLE == 2:
                    _tx0 = -_cw / 2
                elif TABSTYLE == 4:
                    _tx0 = _cw / 2 - _tw
                elif TABSTYLE == 1:
                    _tx0 = -_cw / 2 if _ix == 0 else (_cw / 2 - _tw if _ix == DX - 1 else -_tw / 2)
                else:
                    _tx0 = -_tw / 2
                _th = 0.7265 * 15.85 + 1.2
                _tpts = [(_cd / 2, -_th), (_cd / 2, 0), (_cd / 2 - 15.85, 0), (_cd / 2 - 15.85, -1.2), (_cd / 2, -_th)]
                _tab = extrude(Plane.YZ * make_face(Polyline(*_tpts)), amount=_tw)
                _cut -= Pos(_tx0, 0, 0) * _tab
            result -= Pos(_cx, _cy, _ztop) * _cut
# ---- stacking lip: measured ring profile (outer flush with the walls,
# funnel void, rounded tip; total height H + 3.55) ----
if LIP:
    _lip_prof = [(-1.2, 0.0, 3.75, 2.6, 2.5), (0.0, 0.0, 3.75, 2.6, 2.5), (2.4, 0.04, 3.75, 1.9, 2.5), (3.0, 0.05, 3.75, 1.4, 2.5), (3.3, 0.13, 3.5, 1.1, 2.5), (3.5, 0.37, 3.2, 0.84, 2.0)]
    _ob0 = max(H - 3.0, 6.5)
    _ib0 = max(H - 3.5, 6.5)
    _lo = [Plane.XY.offset(_ob0) * RectangleRounded(W, D, 3.75)] + [Plane.XY.offset(H + _dz) * RectangleRounded(W - 2 * _oi, D - 2 * _oi, _or) for _dz, _oi, _or, _vi, _vr in _lip_prof]
    _li = [Plane.XY.offset(_ib0) * RectangleRounded(W - 2 * 1.25, D - 2 * 1.25, 2.5)] + [Plane.XY.offset(H + _dz) * RectangleRounded(W - 2 * _vi, D - 2 * _vi, _vr) for _dz, _oi, _or, _vi, _vr in _lip_prof]
    _lip_outer = loft(Sketch() + _lo + [Plane.XY.offset(H + 3.55) * RectangleRounded(W - 1.1, D - 1.1, 3.0)], ruled=True)
    _lip_inner = loft(Sketch() + _li, ruled=True)
    result += _lip_outer - _lip_inner
"""


tube_SPEC = {'label': 'Tube', 'blurb': 'Hollow cylinder.', 'params': [{'key': 'R_OUT', 'label': 'Outer radius', 'unit': 'mm', 'ptype': 'number', 'default': 12, 'min': 1.0, 'max': 150.0, 'step': 0.5}, {'key': 'R_IN', 'label': 'Inner radius', 'unit': 'mm', 'ptype': 'number', 'default': 8, 'min': 0.5, 'max': 149.0, 'step': 0.5}, {'key': 'H', 'label': 'Height', 'unit': 'mm', 'ptype': 'number', 'default': 25, 'min': 1.0, 'max': 300.0, 'step': 0.5}]}


tube_TEMPLATE = r"""
# object: Tube
# blurb: Hollow cylinder.
# Tube for orcad — runnable build123d program (see header above).
from build123d import *

R_OUT = 12  # spec: number label=Outer radius unit=mm min=1 max=150 step=0.5
R_IN = 8  # spec: number label=Inner radius unit=mm min=0.5 max=149 step=0.5
H = 25  # spec: number label=Height unit=mm min=1 max=300 step=0.5

result = Cylinder(R_OUT, H) - Cylinder(R_IN, H + 2)
"""


PRIMITIVES = {
    "box": box_SPEC,
    "bracket": bracket_SPEC,
    "cylinder": cylinder_SPEC,
    "gridfinity_baseplate": gridfinity_baseplate_SPEC,
    "gridfinity_bin": gridfinity_bin_SPEC,
    "tube": tube_SPEC,
}


_TEMPLATES = {
    "box": box_TEMPLATE,
    "bracket": bracket_TEMPLATE,
    "cylinder": cylinder_TEMPLATE,
    "gridfinity_baseplate": gridfinity_baseplate_TEMPLATE,
    "gridfinity_bin": gridfinity_bin_TEMPLATE,
    "tube": tube_TEMPLATE,
}


def generate_primitive_code(primitive, params):
    """Return the object program with params baked into `# spec:` lines."""
    c = validate_primitive_params(primitive, params)
    try:
        template = _TEMPLATES[primitive]
    except KeyError:
        raise ValueError(f"unknown object {primitive!r}") from None
    return _bake_template(template, c)

# END BUNDLED OBJECTS


def _defaults(primitive):
    out = {}
    for p in PRIMITIVES[primitive]["params"]:
        out[p["key"]] = p["default"]
    return out


EXAMPLES = {
    "calibration_cube": {
        "label": "Calibration cube (20mm)",
        "code": (
            "from build123d import *\n"
            "result = Box(20, 20, 20)\n"
        ),
    },
    "bracket": {
        "label": "Bracket plate with holes",
        "code": (
            "from build123d import *\n"
            "L, W, T, D = 60, 30, 5, 5\n"
            "plate = Box(L, W, T)\n"
            "hole = Cylinder(D / 2, T + 2)\n"
            "h1 = Pos(-L / 4, 0, -1) * hole\n"
            "h2 = Pos(L / 4, 0, -1) * hole\n"
            "result = plate - h1 - h2\n"
        ),
    },
    "tube_demo": {
        "label": "Tube + top ring",
        "code": (
            "from build123d import *\n"
            "outer = Cylinder(12, 25)\n"
            "inner = Cylinder(8, 27)\n"
            "tube = outer - inner\n"
            "ring = Pos(0, 0, 25) * Cylinder(10, 3)\n"
            "result = tube + ring\n"
        ),
    },
    # Generated from the same codegen as the objects dropdown (no drift).
    "gridfinity_bin_2x2x6": {
        "label": "Gridfinity bin 2x2x6u",
        "code": None,  # filled below
    },
}

EXAMPLES["gridfinity_bin_2x2x6"]["code"] = generate_primitive_code(
    "gridfinity_bin", _defaults("gridfinity_bin"))


def sanitize_stem(name):
    s = re.sub(r"[^A-Za-z0-9_-]+", "_", str(name or "model")).strip("_")
    return (s or "model")[:60]


def stamped_filename(stem, ext):
    ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    return f"{sanitize_stem(stem)}_{ts}.{ext}"


def exports_dir():
    """Plugin-local exports dir (inside data_dir allowed root, no prompt)."""
    try:
        base = Path(__file__).resolve().parent
    except NameError:  # pragma: no cover - defensive (tests always have __file__)
        base = Path.cwd()
    d = base / "exports"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ---------------------------------------------------------------------------
# Runner (build123d imported lazily so tests + plugin load stay light)
# ---------------------------------------------------------------------------

def _find_result(namespace):
    for key in ("result", "part", "solid", "model", "output"):
        obj = namespace.get(key)
        if obj is not None and not isinstance(obj, (int, float, str, bool, list, dict)):
            return obj, key
    # fallback: first object that looks like a Shape
    try:
        from build123d.topology import Shape
    except Exception:
        return None, None
    for key, obj in namespace.items():
        if key.startswith("_"):
            continue
        if isinstance(obj, Shape):
            return obj, key
    return None, None


def _shape_stats(shape):
    stats = {}
    for attr in ("volume", "area"):
        with suppress(Exception):
            value = cast(Any, getattr(shape, attr, None))
            stats[attr + "_mm"] = round(_num(value() if callable(value) else value, attr), 3)
    with suppress(Exception):
        bb = shape.bounding_box()
        stats["bbox_min"] = [round(_num(x, "bbox"), 3) for x in bb.min]
        stats["bbox_max"] = [round(_num(x, "bbox"), 3) for x in bb.max]
        stats["bbox_size"] = [round(_num(x, "bbox"), 3) for x in bb.size]
    return stats


def _preview_payload(shape, tolerance=DEFAULT_TOLERANCE):
    """Decimated triangle soup for the page's 3D preview. Best-effort: None on failure.

    Returns {"tris": [x1,y1,z1, ...], "shown": n, "total": m} or None.
    """
    try:
        vertices, triangles = shape.tessellate(_num(tolerance, "tolerance"), 0.1)
        total = len(triangles)
        if total == 0:
            return None
        step = max(1, total // PREVIEW_MAX_TRIS)
        sample = triangles[::step]
        flat = []
        for tri in sample:
            for idx in (int(tri[0]), int(tri[1]), int(tri[2])):
                v = vertices[idx]
                flat.extend([round(float(v.X), 3), round(float(v.Y), 3), round(float(v.Z), 3)])
        return {"tris": flat, "shown": len(sample), "total": total}
    except Exception:
        return None


def _execute_code(code):
    """Import build123d, exec code, return (shape, var). Raises RuntimeError."""
    try:
        import build123d  # noqa: F401  (ensures dependency present)
    except Exception as exc:
        raise RuntimeError(
            "build123d is not installed in Orca's Python environment yet. "
            "Reopen Plugins dialog / restart OrcaSlicer so bundled `uv` can "
            f"install dependencies, then retry. ({exc})"
        ) from exc

    namespace = {"__name__": "__orcad__"}
    try:
        exec("from build123d import *", namespace)  # noqa: S102 - intentional CAD exec
        exec(code, namespace)  # noqa: S102 - intentional CAD exec
    except Exception as exc:
        tb = traceback.format_exc(limit=8)
        raise RuntimeError(f"CAD code failed: {exc}\n{tb}") from exc

    shape, var = _find_result(namespace)
    if shape is None:
        raise RuntimeError(
            "No result found. Assign your final solid to variable `result` "
            "(e.g. `result = Box(20, 20, 20)`)."
        )
    # volume sanity (2D sketches have ~0 volume -> STL would be empty)
    try:
        shape_obj = cast(Any, shape)
        vol = _num(shape_obj.volume() if callable(getattr(shape_obj, "volume", None))
                   else shape_obj.volume, "volume")
        if vol <= 0:
            raise RuntimeError(
                f"`{var}` has zero volume ({vol}). STL/3MF need a solid — "
                "did you build a flat sketch? Extrude it first."
            )
    except RuntimeError:
        raise
    except Exception:
        return shape, var  # best-effort only; let exporter decide
    return shape, var


def _tolerance(value):
    tol = _num(value, "tolerance")
    if not (0.0001 <= tol <= 1.0):
        raise ValueError("tolerance must be within [0.0001, 1.0]")
    return tol


def preview_shape(code, tolerance=DEFAULT_TOLERANCE):
    """Execute code, return stats + preview payload WITHOUT exporting.

    Returns dict (JSON-able). Raises RuntimeError on failure.
    """
    tol = _tolerance(tolerance)
    if len(code) > 200_000:
        raise ValueError("code too large (>200k chars)")
    shape, var = _execute_code(code)
    return {"ok": True, "var": var, "stats": _shape_stats(shape),
            "preview": _preview_payload(shape, tol)}


def _open_with_default_app(path):
    """Open a file with the OS default app (OrcaSlicer for .stl when associated).

    This is how "send to plate" works: OrcaSlicer's single-instance handling
    forwards the file to the running instance, which loads it onto the plate.
    Spawning the opener is ProcessCreate-audited: the user approves it once
    per plugin (remembered in .install_state.json).
    """
    s = str(path)
    try:
        if sys.platform.startswith("win"):
            cast(Any, os).startfile(s)  # noqa: S606 - user-approved local file open
        elif sys.platform == "darwin":
            subprocess.Popen(["open", s])
        else:
            try:
                subprocess.Popen(["xdg-open", s])
            except FileNotFoundError:
                subprocess.Popen(["gio", "open", s])
    except Exception as exc:
        raise RuntimeError(
            f"Could not hand {s} to OrcaSlicer ({exc}). "
            "Drag the file from exports/ onto Prepare instead.") from exc


def run_build123d_code(code, export_format="stl", tolerance=DEFAULT_TOLERANCE,
                       filename_stem="model"):
    """Execute user code, export result. Returns dict (JSON-able). No orca needed.

    Raises RuntimeError with user-facing message on failure.
    """
    if export_format not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of {EXPORT_FORMATS}")
    tol = _tolerance(tolerance)
    if not (0.0001 <= tol <= 1.0):
        raise ValueError("tolerance must be within [0.0001, 1.0]")
    if len(code) > 200_000:
        raise ValueError("code too large (>200k chars)")

    shape, var = _execute_code(code)

    out_dir = exports_dir()
    filename = stamped_filename(filename_stem, export_format)
    out_path = out_dir / filename
    try:
        if export_format == "stl":
            from build123d import export_stl
            ok = export_stl(shape, str(out_path), tolerance=tol)
            if not ok:
                raise RuntimeError("export_stl reported failure")
        elif export_format == "step":
            from build123d import export_step
            ok = export_step(shape, str(out_path))
            if not ok:
                raise RuntimeError("export_step reported failure")
        else:  # 3mf via Mesher
            from build123d import Mesher
            m = Mesher()
            m.add_shape(shape)
            m.write(str(out_path))
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Export to {export_format} failed: {exc}") from exc

    try:
        size = out_path.stat().st_size
    except Exception:
        size = -1
    return {
        "ok": True,
        "file": str(out_path),
        "filename": filename,
        "format": export_format,
        "var": var,
        "size_bytes": size,
        "stats": _shape_stats(shape),
        "preview": _preview_payload(shape, tol),
    }


# ---------------------------------------------------------------------------
# Page UI — compact inline app with one Three.js rendering dependency.
# Orca's WebView does not reliably load external stylesheets, so the adaptive
# theme and responsive layout ship in the page.
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>orcad</title>
<style>
:root{color-scheme:light dark;--bg:var(--orca-bg,#101318);--panel:var(--orca-bg,#181c23);--panel2:var(--orca-bg,#20252e);--fg:var(--orca-fg,#eef1f5);--muted:var(--orca-muted,#8c96a4);--line:var(--orca-border,#303744);--accent:var(--orca-accent,#28c2ad);--accentfg:var(--orca-accent-fg,#062b27);--danger:#f06a72;--font:var(--orca-font,system-ui,-apple-system,"Segoe UI",sans-serif);--mono:ui-monospace,SFMono-Regular,Consolas,monospace}
@media(prefers-color-scheme:light){:root{--bg:var(--orca-bg,#f3f5f8);--panel:var(--orca-bg,#fff);--panel2:var(--orca-bg,#e9edf2);--fg:var(--orca-fg,#20252c);--muted:var(--orca-muted,#697482);--line:var(--orca-border,#d9dee6);--accent:var(--orca-accent,#087e6e);--accentfg:#fff}}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.45 var(--font)}button,input,select,textarea{font:inherit}button{cursor:pointer}button:focus-visible,input:focus-visible,select:focus-visible,textarea:focus-visible{outline:2px solid var(--accent);outline-offset:1px}.mono{font-family:var(--mono)}.muted{color:var(--muted)}
.appbar{height:52px;display:flex;align-items:center;gap:10px;padding:0 16px;background:var(--panel);border-bottom:1px solid var(--line)}.logo{font-weight:750;letter-spacing:.02em}.logo span{color:var(--accent);font-weight:450}.status{font-size:12px;color:var(--muted)}.grow{flex:1}
.btn{border:1px solid var(--line);border-radius:7px;background:transparent;color:var(--fg);padding:6px 10px;font-weight:600}.btn:hover{border-color:var(--accent)}.btn.primary{border-color:var(--accent);background:var(--accent);color:var(--accentfg)}.btn.small{padding:4px 8px;font-size:12px}.btn:disabled{opacity:.5;cursor:default}select,input[type=number],input[type=text]{border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);padding:6px 8px}.format{width:76px}.tol{width:80px}
.shell{max-width:1500px;margin:0 auto;padding:14px;display:grid;grid-template-columns:310px minmax(0,1fr);gap:14px}.side,.card{background:var(--panel);border:1px solid var(--line);border-radius:10px}.side{overflow:hidden;align-self:start}.tabs{display:grid;grid-template-columns:1fr 1fr;padding:5px;gap:4px;border-bottom:1px solid var(--line)}.tabs button{border:0;border-radius:6px;background:transparent;color:var(--muted);padding:7px;font-weight:650}.tabs button.on{background:var(--accent);color:var(--accentfg)}.sidebody{padding:12px}.label{display:block;color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;margin:0 0 6px}.search{width:100%;margin-bottom:8px}.object{width:100%;min-height:36px}.blurb{font-size:12px;color:var(--muted);margin:8px 1px 14px}.section-title{color:var(--muted);font-size:11px;font-weight:700;letter-spacing:.08em;text-transform:uppercase;margin:16px 0 5px}.param{padding:8px 0;border-bottom:1px dashed var(--line)}.param:last-child{border:0}.paramhead{display:flex;align-items:center;justify-content:space-between;gap:8px}.param label{font-size:12px}.param b{font-weight:650}.unit{color:var(--muted);font-size:11px;margin-left:4px}.param input[type=number]{width:76px;text-align:right;padding:4px 6px}.param input[type=range]{display:block;width:100%;margin:7px 0 0;accent-color:var(--accent)}.param input[type=checkbox]{width:18px;height:18px;accent-color:var(--accent)}.editor{width:100%;height:410px;resize:vertical;border:1px solid var(--line);border-radius:7px;background:var(--bg);color:var(--fg);padding:10px;font:12px/1.5 var(--mono)}.editor-actions{display:flex;gap:6px;margin-top:8px}.editor-actions select{min-width:0;flex:1}
.main{min-width:0;display:grid;gap:14px}.viewer{padding:0;overflow:hidden}.viewerbar{height:46px;display:flex;align-items:center;gap:7px;padding:0 10px;border-bottom:1px solid var(--line)}.viewerbar strong{font-size:13px}.viewerbar .muted{font-size:12px}.viewerhost{position:relative;height:510px;background:radial-gradient(circle at 50% 35%,rgba(60,80,100,.23),transparent 65%),var(--bg)}#pv3d{width:100%;height:100%;touch-action:none}#pv3d canvas{display:block;width:100%;height:100%;cursor:grab}.empty{position:absolute;inset:0;display:grid;place-items:center;text-align:center;color:var(--muted);pointer-events:none}.empty strong{display:block;color:var(--fg);margin-bottom:3px}.empty.hidden{display:none}.viewerfoot{display:flex;align-items:center;gap:10px;padding:8px 10px;border-top:1px solid var(--line);font-size:12px}.viewerfoot .mono{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.stats{display:flex;gap:6px;flex-wrap:wrap}.stat{background:var(--panel2);border-radius:5px;padding:2px 6px}.stat b{font-family:var(--mono);font-weight:500}
.bottom{display:grid;grid-template-columns:1fr 1fr;gap:14px}.card{padding:12px}.card h2{font-size:12px;margin:0 0 8px}.result{min-height:76px;font-size:12px}.kv{display:grid;grid-template-columns:80px 1fr;gap:3px 8px}.kv .k{color:var(--muted)}.kv .v{font-family:var(--mono);word-break:break-all}.log{height:76px;overflow:auto;white-space:pre-wrap;background:var(--bg);border:1px solid var(--line);border-radius:7px;padding:7px;font:11px/1.4 var(--mono)}.error{color:var(--danger);white-space:pre-wrap;font:11px/1.4 var(--mono)}
@media(max-width:900px){.shell{grid-template-columns:1fr}.side{order:2}.viewerhost{height:420px}.bottom{grid-template-columns:1fr}}@media(max-width:560px){.appbar{padding:0 9px}.appbar .tol{display:none}.shell{padding:8px}.viewerhost{height:330px}.viewerbar{flex-wrap:wrap;height:auto;padding:8px}.viewerbar .grow{display:none}}
</style>
</head>
<body>
<header class="appbar"><div class="logo">orcad <span>build123d</span></div><div id="status" class="status">ready</div><div class="grow"></div><select id="fmt" class="format" title="Export format"><option value="stl">STL</option><option value="step">STEP</option><option value="3mf">3MF</option></select><input id="tol" class="tol" type="number" value="0.001" step="0.001" min="0.0001" max="1" title="Tessellation tolerance"><button id="runBtn" class="btn primary">Run / export</button></header>
<div class="shell">
<aside class="side"><nav class="tabs"><button id="objectsTab" class="on">Objects</button><button id="codeTab">Code</button></nav>
<section id="objectsPane" class="sidebody"><label class="label" for="objectSearch">Model</label><input id="objectSearch" class="search" type="text" placeholder="Filter objects…" autocomplete="off"><select id="objectSelect" class="object"></select><div id="blurb" class="blurb"></div><div class="section-title">Parameters</div><div id="params"></div><button id="generateBtn" class="btn primary" style="width:100%;margin-top:12px">Generate + export</button></section>
<section id="codePane" class="sidebody" hidden><label class="label" for="code">build123d code</label><textarea id="code" class="editor" spellcheck="false"></textarea><div class="editor-actions"><select id="exampleSelect"></select><button id="loadExample" class="btn small">Load</button><button id="runCode" class="btn primary small">Run</button></div></section>
</aside>
<main class="main"><section class="card viewer"><div class="viewerbar"><strong>Preview</strong><span id="previewStatus" class="muted">waiting for a model</span><div class="grow"></div><button id="resetView" class="btn small">Reset</button><button id="wireBtn" class="btn small">Wireframe</button><button id="spinBtn" class="btn small">Spin: on</button><button id="plateBtn" class="btn primary small">Send to plate</button></div><div id="viewerHost" class="viewerhost"><div id="pv3d"></div><div id="empty" class="empty"><div><strong>Nothing previewed yet</strong><span>Choose a model and adjust a parameter.</span></div></div></div><div class="viewerfoot"><div id="stats" class="stats"></div><div class="grow"></div><span class="muted">drag to rotate · wheel to zoom</span></div></section><div class="bottom"><section class="card"><h2>Last export</h2><div id="result" class="result muted">Nothing exported yet.</div></section><section class="card"><h2>Activity</h2><div id="log" class="log">ready.</div></section></div></main>
</div>
<script src="https://cdn.jsdelivr.net/npm/three@0.160.0/build/three.min.js"></script>
<script>
'use strict';
/* BEGIN OBJECTS SPEC */
var PRIMS={
 box:{label:'Box',blurb:'Simple centered block.',params:[['L','Length','mm','number',20,1.0,300.0,0.5],['W','Width','mm','number',20,1.0,300.0,0.5],['H','Height','mm','number',20,1.0,300.0,0.5]]},
 bracket:{label:'Bracket plate',blurb:'Flat plate with two holes.',params:[['L','Length','mm','number',60,10.0,300.0,0.5],['W','Width','mm','number',30,10.0,200.0,0.5],['T','Thickness','mm','number',5,1.0,50.0,0.5],['D','Hole dia','mm','number',5,1.0,50.0,0.5]]},
 cylinder:{label:'Cylinder',blurb:'Round post or puck.',params:[['R','Radius','mm','number',10,0.5,150.0,0.5],['H','Height','mm','number',20,1.0,300.0,0.5]]},
 gridfinity_baseplate:{label:'Gridfinity Baseplate',blurb:'Grid the bins snap into, with sockets + magnet holes.',params:[['GX','Grid X','u','int',4,1.0,6.0,1.0],['GY','Grid Y','u','int',4,1.0,6.0,1.0],['T','Thickness','mm','number',5,4.6,8.0,0.2],['STYLE','Plate style','','int',0,0.0,4.0,1.0],['HOLESTYLE','Mount holes','','int',0,0.0,2.0,1.0],['DISTX','Minimum X','mm','number',0,0.0,300.0,1.0],['DISTY','Minimum Y','mm','number',0,0.0,300.0,1.0],['FITX','Fit X','','number',0,-1.0,1.0,0.1],['FITY','Fit Y','','number',0,-1.0,1.0,0.1],['SCREW_D','Screw diameter','mm','number',3.35,2.0,5.0,0.05],['SCREW_HEAD','Screw head diameter','mm','number',5,3.0,8.0,0.1],['SCREW_SPACING','Screw spacing','mm','number',0.5,0.0,2.0,0.1],['NSCREWS','Screws per seam','','int',1,1.0,3.0,1.0],['SOCKETS','Bin sockets','','bool',1],['REFINED','Refined holes','','bool',0],['MAGNETS','Magnet holes (6x2)','','bool',1],['SCREW','Screw holes (M3)','','bool',0],['CRUSH','Crush ribs','','bool',1],['CHAMFER','Hole chamfer','','bool',1],['PRINTABLE','Supportless hole tops','','bool',0],['CORNERS','Holes only at corners','','bool',0]]},
 gridfinity_bin:{label:'Gridfinity Bin',blurb:'Full Rebuilt port: compartments, tabs, scoop, holes, lip, height modes.',params:[['GX','Grid X','u','int',2,1.0,6.0,1.0],['GY','Grid Y','u','int',2,1.0,6.0,1.0],['HU','Height value','','int',6,0.0,200.0,1.0],['HMODE','Height mode 0U 1in 2ex 3exlip','','int',0,0.0,3.0,1.0],['ZS','Snap height to 7mm','','bool',0],['FILL','Solid fill mm (0=auto)','mm','number',0,0.0,200.0,1.0],['WALL','Outer wall','mm','number',0.95,0.95,2.4,0.05],['DX','Divisions X (0=solid)','','int',1,0.0,6.0,1.0],['DY','Divisions Y (0=solid)','','int',1,0.0,6.0,1.0],['DEPTH','Compartment depth mm (0=full)','mm','number',0,0.0,200.0,1.0],['SCOOPW','Scoop amount','','number',1.0,0.0,1.0,0.1],['TABSTYLE','Tab 0Full 1Auto 2Left 3Center 4Right 5None','','int',1,0.0,5.0,1.0],['TABPLACE','Tabs only top-left','','int',0,0.0,1.0,1.0],['CYL','Cylindrical compartments','','bool',0],['CD','Cylinder dia','mm','number',10,1.0,60.0,0.5],['CCHAM','Cylinder top chamfer','mm','number',0.5,0.0,5.0,0.1],['REFINED','Refined holes','','bool',1],['MAGNETS','Magnet holes (6x2)','','bool',0],['SCREW','Screw holes (M3)','','bool',0],['CRUSH','Crush ribs','','bool',1],['CHAMFER','Hole chamfer','','bool',1],['PRINTABLE','Supportless hole tops','','bool',1],['CORNERS','Holes only at corners','','bool',0],['THUMB','Thumbscrew holes','','bool',0],['LIP','Stacking lip','','bool',1]]},
 tube:{label:'Tube',blurb:'Hollow cylinder.',params:[['R_OUT','Outer radius','mm','number',12,1.0,150.0,0.5],['R_IN','Inner radius','mm','number',8,0.5,149.0,0.5],['H','Height','mm','number',25,1.0,300.0,0.5]]}
};
/* END OBJECTS SPEC */
var EXAMPLES={calibration_cube:'from build123d import *\nresult = Box(20, 20, 20)\n',bracket:'from build123d import *\nL, W, T, D = 60, 30, 5, 5\nplate = Box(L, W, T)\nhole = Cylinder(D / 2, T + 2)\nh1 = Pos(-L / 4, 0, -1) * hole\nh2 = Pos(L / 4, 0, -1) * hole\nresult = plate - h1 - h2\n',tube_demo:'from build123d import *\nouter = Cylinder(12, 25)\ninner = Cylinder(8, 27)\ntube = outer - inner\nring = Pos(0, 0, 25) * Cylinder(10, 3)\nresult = tube + ring\n',gridfinity_bin_2x2x6:'(generated — choose Gridfinity Bin and click Generate)'};
var S={mode:'objects',primitive:'gridfinity_bin',params:{},seq:0,timer:null,wire:false,spin:true};
function $(id){return document.getElementById(id)}function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]})}function log(s){var e=$('log');e.textContent+=s+'\n';e.scrollTop=e.scrollHeight}function status(s){$('status').textContent=s}function send(o){try{window.orca.postMessage(o)}catch(e){log('bridge: '+e)}}function tolerance(){return parseFloat($('tol').value)||.001}
function currentPayload(command){if(S.mode==='objects')return{command:command,kind:'generate',primitive:S.primitive,params:S.params,format:'stl',tolerance:tolerance(),filename:S.primitive};return{command:command,kind:'run',code:$('code').value,format:$('fmt').value,tolerance:tolerance(),filename:'model'}}
function renderObjectList(){var q=$('objectSearch').value.toLowerCase(),sel=$('objectSelect'),old=S.primitive;sel.innerHTML='';Object.keys(PRIMS).forEach(function(k){var p=PRIMS[k];if(q&&k.indexOf(q)<0&&p.label.toLowerCase().indexOf(q)<0)return;var o=document.createElement('option');o.value=k;o.textContent=p.label;sel.appendChild(o)});if(!sel.options.length)return;sel.value=PRIMS[S.primitive]&&(!q||sel.querySelector('option[value="'+S.primitive+'"]'))?S.primitive:sel.options[0].value;if(sel.value!==old){S.primitive=sel.value;S.params={}}}
function updateParam(k,v){S.params[k]=v;sendCode();schedulePreview()}
function renderParams(){var p=PRIMS[S.primitive],host=$('params');$('blurb').textContent=p.blurb||'';host.innerHTML='';p.params.forEach(function(a){var k=a[0],row=document.createElement('div');row.className='param';var head=document.createElement('div');head.className='paramhead';var lab=document.createElement('label');lab.innerHTML='<b>'+esc(k)+'</b> '+esc(a[1])+'<span class="unit">'+esc(a[2])+'</span>';head.appendChild(lab);var val=S.params[k]!=null?S.params[k]:a[4];S.params[k]=val;if(a[3]==='bool'){var cb=document.createElement('input');cb.type='checkbox';cb.checked=!!val;cb.onchange=function(){updateParam(k,cb.checked)};head.appendChild(cb);row.appendChild(head)}else{var num=document.createElement('input');num.type='number';num.min=a[5];num.max=a[6];num.step=a[7];num.value=val;var rng=document.createElement('input');rng.type='range';rng.min=a[5];rng.max=a[6];rng.step=a[7];rng.value=val;function set(v){var n=a[3]==='int'?parseInt(v,10):parseFloat(v);if(isNaN(n))return;S.params[k]=n;num.value=n;rng.value=n;sendCode();schedulePreview()}num.oninput=function(){set(num.value)};rng.oninput=function(){set(rng.value)};head.appendChild(num);row.appendChild(head);row.appendChild(rng)}host.appendChild(row)})}
function sendCode(){send({command:'code',kind:'generate',primitive:S.primitive,params:S.params})}
function schedulePreview(){if(S.mode!=='objects')return;clearTimeout(S.timer);S.timer=setTimeout(function(){S.seq++;$('previewStatus').textContent='building preview…';sendCode();var p=currentPayload('preview');p.seq=S.seq;send(p)},420)}
function generate(){var p=currentPayload('generate');status('building…');log('export '+(p.filename||'model'));send(p)}
function runCode(){S.mode='code';var p=currentPayload('run');status('building…');log('run code');send(p)}
function sendPlate(){var p=currentPayload('plate');status('sending…');log('send to plate');send(p)}
function setMode(m){S.mode=m;$('objectsTab').classList.toggle('on',m==='objects');$('codeTab').classList.toggle('on',m==='code');$('objectsPane').hidden=m!=='objects';$('codePane').hidden=m!=='code';if(m==='code')sendCode();else schedulePreview()}
function showResult(d){var e=$('result');if(!d.ok){e.innerHTML='<div class="error">'+esc(d.error||'failed')+'</div>';return}e.innerHTML='<div class="kv"><div class="k">file</div><div class="v">'+esc(d.file)+'</div><div class="k">size</div><div class="v">'+esc(d.size_bytes)+' bytes</div><div class="k">solid</div><div class="v">'+esc(d.var||'result')+'</div></div>';log('ok '+(d.filename||'model'))}
function showStats(stats){var e=$('stats');e.innerHTML='';Object.keys(stats||{}).slice(0,5).forEach(function(k){var s=document.createElement('span');s.className='stat';s.innerHTML=esc(k)+': <b>'+esc(stats[k])+'</b>';e.appendChild(s)})}
function removeMesh(){if(V.mesh){V.scene.remove(V.mesh);V.mesh.geometry.dispose();V.mesh.material.dispose();V.mesh=null}}
function applyPreview(p){$('empty').classList.toggle('hidden',!!(p&&p.tris&&p.tris.length));removeMesh();if(!p||!p.tris||!p.tris.length){$('previewStatus').textContent='no mesh returned';return}if(!V.renderer){$('previewStatus').textContent='WebGL preview unavailable';return}var g=new THREE.BufferGeometry();g.setAttribute('position',new THREE.BufferAttribute(new Float32Array(p.tris),3));g.computeVertexNormals();g.computeBoundingBox();var c=g.boundingBox.getCenter(new THREE.Vector3()),size=g.boundingBox.getSize(new THREE.Vector3()),d=Math.max(size.x,size.y,size.z,1);V.mesh=new THREE.Mesh(g,new THREE.MeshStandardMaterial({color:0x35c4b0,roughness:.75,metalness:.05,wireframe:S.wire}));V.mesh.position.set(-c.x,-c.y,-c.z);V.scene.add(V.mesh);V.size=d;fitView();$('previewStatus').textContent='mesh ready · '+(p.total||p.tris.length/9)+' triangles'}
function fitView(){if(!V.camera||!V.size)return;V.camera.position.set(V.size*1.9,V.size*1.5,V.size*1.9);V.camera.lookAt(0,0,0)}
function initViewer(){var host=$('pv3d');try{if(!window.THREE)throw Error('Three.js did not load');V.scene=new THREE.Scene();V.camera=new THREE.PerspectiveCamera(40,1,.01,100000);V.renderer=new THREE.WebGLRenderer({antialias:true,alpha:true});V.renderer.setPixelRatio(Math.min(window.devicePixelRatio||1,2));host.appendChild(V.renderer.domElement);V.scene.add(new THREE.HemisphereLight(0xffffff,0x263746,2));var light=new THREE.DirectionalLight(0xffffff,2.4);light.position.set(70,100,80);V.scene.add(light);var drag=null;host.addEventListener('pointerdown',function(e){drag={x:e.clientX,y:e.clientY};try{host.setPointerCapture(e.pointerId)}catch(_){}host.style.cursor='grabbing'});host.addEventListener('pointermove',function(e){if(!drag||!V.mesh)return;V.mesh.rotation.y+=(e.clientX-drag.x)*.01;V.mesh.rotation.x=Math.max(-1.45,Math.min(1.45,V.mesh.rotation.x+(e.clientY-drag.y)*.01));drag={x:e.clientX,y:e.clientY}});['pointerup','pointercancel','pointerleave'].forEach(function(k){host.addEventListener(k,function(){drag=null;host.style.cursor='grab'})});host.addEventListener('wheel',function(e){e.preventDefault();if(!V.camera||!V.size)return;var q=e.deltaY>0?1.1:.9;V.camera.position.multiplyScalar(q);var d=V.camera.position.length();if(d<V.size*.35||d>V.size*8) V.camera.position.setLength(Math.max(V.size*.35,Math.min(V.size*8,d)));V.camera.lookAt(0,0,0)},{passive:false});resizeViewer();window.addEventListener('resize',resizeViewer)}catch(e){$('previewStatus').textContent='preview unavailable';log(String(e));return}requestAnimationFrame(viewerLoop)}
function resizeViewer(){if(!V.renderer)return;var h=$('pv3d'),w=h.clientWidth||640,ht=h.clientHeight||400;V.renderer.setSize(w,ht,false);V.camera.aspect=w/ht;V.camera.updateProjectionMatrix()}
function viewerLoop(){requestAnimationFrame(viewerLoop);if(V.mesh&&S.spin)V.mesh.rotation.y+=.005;V.renderer.render(V.scene,V.camera)}
function handleMessage(d){if(!d)return;if(d.type==='progress'){status(d.message||'working…');return}if(d.type==='code'){if(d.ok&&S.mode==='code')$('code').value=d.code;return}if(d.type==='preview'){if(d.seq!==S.seq)return;if(d.ok){applyPreview(d.preview);showStats(d.stats);$('previewStatus').textContent='preview ready'}else{$('previewStatus').textContent='preview failed';log(d.error||'preview failed')}return}if(d.type==='plate_result'){showResult(d);if(d.ok)applyPreview(d.preview);status(d.ok?'sent':'failed');return}if(d.type==='result'){showResult(d);if(d.ok){applyPreview(d.preview);showStats(d.stats);status('done')}return}if(d.type==='error'||d.ok===false){showResult({ok:false,error:d.error});status('failed')}}
var V={scene:null,camera:null,renderer:null,mesh:null,size:0};
$('objectsTab').onclick=function(){setMode('objects')};$('codeTab').onclick=function(){setMode('code')};$('objectSearch').oninput=function(){renderObjectList();renderParams();schedulePreview()};$('objectSelect').onchange=function(){S.primitive=this.value;S.params={};renderParams();sendCode();schedulePreview()};$('generateBtn').onclick=generate;$('runBtn').onclick=function(){S.mode==='objects'?generate():runCode()};$('runCode').onclick=runCode;$('plateBtn').onclick=sendPlate;$('resetView').onclick=function(){if(V.mesh){V.mesh.rotation.set(0,0,0);fitView()}};$('wireBtn').onclick=function(){S.wire=!S.wire;this.textContent='Wireframe: '+(S.wire?'on':'off');if(V.mesh)V.mesh.material.wireframe=S.wire};$('spinBtn').onclick=function(){S.spin=!S.spin;this.textContent='Spin: '+(S.spin?'on':'off')};$('loadExample').onclick=function(){$('code').value=EXAMPLES[$('exampleSelect').value]||''};
Object.keys(EXAMPLES).forEach(function(k){var o=document.createElement('option');o.value=k;o.textContent=k;$('exampleSelect').appendChild(o)});if(window.orca&&window.orca.onMessage)window.orca.onMessage(handleMessage);renderObjectList();renderParams();initViewer();sendCode();schedulePreview();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Plugin wiring (only when running inside OrcaSlicer)
# ---------------------------------------------------------------------------

def _build_code_from_msg(msg, cmd):
    """Return (code, stem) for run/generate-style messages. Raises ValueError."""
    if cmd == "generate":
        code = generate_primitive_code(
            str(msg.get("primitive", "box")), dict(msg.get("params", {})))
        stem = str(msg.get("filename") or msg.get("primitive") or "model")
    else:
        code = str(msg.get("code", ""))
        if not code.strip():
            raise ValueError("code is empty")
        stem = str(msg.get("filename") or "model")
    return code, stem


def _handle_message_sync(capability, msg):
    """Route one JS message; heavy work runs in a worker thread."""
    if not isinstance(msg, dict):
        return {"type": "error", "ok": False, "error": "message must be an object"}
    cmd = msg.get("command")
    if cmd == "ping":
        return {"type": "pong", "ok": True}
    if cmd == "code":
        # Editor mirror: pure codegen, no CAD run, no thread, instant.
        try:
            code, _stem = _build_code_from_msg(msg, str(msg.get("kind", "generate")))
        except Exception as exc:
            return {"type": "code", "ok": False, "error": str(exc)}
        return {"type": "code", "ok": True, "code": code}
    if cmd in ("run", "generate"):
        try:
            export_format = str(msg.get("format", "stl")).lower()
            tolerance = float(msg.get("tolerance", DEFAULT_TOLERANCE))
            code, stem = _build_code_from_msg(msg, cmd)
        except Exception as exc:
            return {"type": "error", "ok": False, "error": str(exc)}

        def _work():
            try:
                capability.post_message({"type": "progress", "message": "Running build123d…"})
                res = run_build123d_code(code, export_format, tolerance, stem)
                capability.post_message({
                    "type": "result", "ok": True,
                    "file": res["file"], "filename": res["filename"],
                    "format": res["format"], "var": res["var"],
                    "size_bytes": res["size_bytes"], "stats": res["stats"],
                    "preview": res.get("preview"),
                })
            except Exception as exc:
                capability.post_message({"type": "error", "ok": False, "error": str(exc)[:4000]})

        threading.Thread(target=_work, name="orcad-export", daemon=True).start()
        return {"type": "progress", "message": "Started…"}
    if cmd == "preview":
        # Live preview: run CAD, post mesh, export nothing. Stale requests
        # (slider moved again while building) are dropped via the seq guard.
        try:
            tolerance = float(msg.get("tolerance", DEFAULT_TOLERANCE))
            code, _stem = _build_code_from_msg(msg, str(msg.get("kind", "generate")))
        except Exception as exc:
            return {"type": "preview", "ok": False, "error": str(exc)}
        seq = msg.get("seq", 0)
        capability._orcad_preview_seq = seq

        def _work():
            try:
                res = preview_shape(code, tolerance)
                if getattr(capability, "_orcad_preview_seq", None) != seq:
                    return
                capability.post_message({"type": "preview", "ok": True, "seq": seq,
                                         "var": res["var"], "stats": res["stats"],
                                         "preview": res.get("preview")})
            except Exception as exc:
                if getattr(capability, "_orcad_preview_seq", None) != seq:
                    return
                capability.post_message({"type": "preview", "ok": False, "seq": seq,
                                         "error": str(exc)[:2000]})

        threading.Thread(target=_work, name="orcad-preview", daemon=True).start()
        return None
    if cmd == "plate":
        # Send to plate: export STL, then open it with the OS default app so
        # OrcaSlicer's single-instance handling loads it onto the build plate.
        try:
            tolerance = float(msg.get("tolerance", DEFAULT_TOLERANCE))
            code, stem = _build_code_from_msg(msg, str(msg.get("kind", "generate")))
        except Exception as exc:
            return {"type": "plate_result", "ok": False, "error": str(exc)}

        def _work():
            try:
                capability.post_message({"type": "progress", "message": "Building STL for plate…"})
                res = run_build123d_code(code, "stl", tolerance, stem)
                capability.post_message({"type": "progress", "message": "Opening in OrcaSlicer…"})
                _open_with_default_app(res["file"])
                capability.post_message({"type": "plate_result", "ok": True,
                                         "file": res["file"], "filename": res["filename"],
                                         "size_bytes": res["size_bytes"], "stats": res["stats"],
                                         "preview": res.get("preview")})
            except Exception as exc:
                capability.post_message({"type": "plate_result", "ok": False,
                                         "error": str(exc)[:4000]})

        threading.Thread(target=_work, name="orcad-plate", daemon=True).start()
        return {"type": "progress", "message": "Started…"}
    return {"type": "error", "ok": False, "error": f"unknown command {cmd!r}"}


if orca is not None:  # pragma: no cover - only inside OrcaSlicer
    _PagesBase = getattr(getattr(orca, "pages", None), "PagesPluginCapabilityBase", None)

    if _PagesBase is not None:
        class CadPage(_PagesBase):
            def get_name(self):
                return "orcad"

            def get_icon(self):
                return ""

            def get_ui(self):
                return PAGE_HTML

            def on_message(self, msg):
                try:
                    if isinstance(msg, str):
                        with suppress(Exception):
                            msg = json.loads(msg)
                    res = _handle_message_sync(self, msg)
                    # Worker commands post their own result; sync commands get an ack.
                    if not (isinstance(res, dict) and res.get("type") == "progress" \
                            and res.get("message") == "Started…") and isinstance(res, dict):
                        self.post_message(res)
                except Exception as exc:
                    with suppress(Exception):
                        self.post_message({"type": "error", "ok": False,
                                           "error": f"handler failed: {exc}"})

            def get_default_config(self):
                return {"tolerance": DEFAULT_TOLERANCE, "format": "stl"}

        @orca.plugin
        class OrcadPagePlugin(orca.base):
            def register_capabilities(self):
                orca.register_capability(CadPage)  # type: ignore[attr-defined]
    else:
        # Fallback for Orca builds without orca.pages: visible upgrade hint.
        class CadScriptFallback(orca.script.ScriptPluginCapabilityBase):
            def get_name(self):
                return "orcad (needs Pages build)"

            def execute(self):
                return orca.ExecutionResult.failure(  # type: ignore[attr-defined]
                    orca.PluginResult.RecoverableError,  # type: ignore[attr-defined]
                    "orcad needs OrcaSlicer Nightly with orca.pages "
                    "(Pages tab API). Please update OrcaSlicer.",
                )

        @orca.plugin
        class OrcadScriptPlugin(orca.base):
            def register_capabilities(self):
                orca.register_capability(CadScriptFallback)  # type: ignore[attr-defined]
