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

Layout (v0.8):
- Left: tabbed panel switching between "Objects" (searchable predefined-object
  dropdown + styled parameter sliders, live preview while editing; the Code
  Editor mirrors the selected object + params) and "Code" (a reliable native
  build123d textarea editor).
- Right: persistent Three.js 3D preview (always visible, live for objects) + result + log.
- "Send to plate": exports an STL and sends an OS open request. The host
  may import it onto the build plate, but this plugin cannot confirm that import.
- Predefined objects: Box, Cylinder, Tube, Bracket, Gridfinity Bin,
  Gridfinity Baseplate. Gridfinity geometry follows the public spec
  (42mm grid, 7mm height units, 0.5 tolerance, stacking foot + optional lip
  and magnet holes) with a simplified stepped foot/lip profile.
- Exports go to <plugin_dir>/exports (inside Orca data_dir, no audit prompt).
  Manual import into plater (orca.host is read-only): drag the exported file
  onto the plater.

The UI is a compiled local Vue/Tailwind app loaded from
`frontend/dist/index.html` when present; a compressed copy is embedded for
single-file installs. The asset includes Three.js and renders the server
tessellation with native pointer rotation and wheel zoom.

Tested target: OrcaSlicer Nightly / >2.4.2 with `orca.pages` (main branch).
On older builds without orca.pages, falls back to a Script capability that
shows an upgrade message.
"""

import base64
import datetime
import gzip
import json
import math
import os
import re
import subprocess
import sys
import tempfile
import threading
import traceback
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import Any, cast

try:
    import orca  # type: ignore[import-not-found]  # provided by OrcaSlicer embedded interpreter
except ImportError:  # pragma: no cover - allows unit tests without Orca
    orca = None

PLUGIN_VERSION = "0.6.0"
EXPORT_FORMATS = ("stl", "step", "3mf")
DEFAULT_TOLERANCE = 0.001

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
#              "min","max","step","default","options","hint"}; options
# are [{"value": implementation_value, "label": display_label}]
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


class ParameterValidationError(ValueError):
    """A user-fixable parameter error with control-level associations."""

    def __init__(self, errors):
        self.errors = tuple(errors)
        message = "; ".join(dict.fromkeys(error["message"] for error in self.errors))
        super().__init__(message)


def _parameter_error(field, message):
    return ParameterValidationError(({"field": field, "message": message},))


def _validation_errors(errors, fields, message):
    for field in fields:
        error = {"field": field, "message": message}
        if error not in errors:
            errors.append(error)


def _gridfinity_bin_height(params):
    raw = (params["HU"] * 7.0 if params["HMODE"] == 0 else
           (params["HU"] + 7.0 if params["HMODE"] == 1 else
            (params["HU"] if params["HMODE"] == 2 else params["HU"] - 4.4)))
    if params["ZS"]:
        raw = raw if raw % 7 == 0 else raw + 7 - raw % 7
    return max(raw, 7.0)


def validate_primitive_params(primitive, params):
    """Return cleaned params or raise a field-level validation error."""
    if primitive not in PRIMITIVES:
        raise ValueError(f"unknown object {primitive!r}")
    cleaned = {}
    for p in PRIMITIVES[primitive]["params"]:
        key = p["key"]
        ptype = p.get("ptype", "number")
        if key not in params:
            raise _parameter_error(key, f"missing parameter {key}")
        raw = params[key]
        if ptype == "bool":
            try:
                cleaned[key] = _bool(raw, key)
            except ValueError as exc:
                raise _parameter_error(key, str(exc)) from None
            continue
        try:
            v = _num(raw, key)
        except ValueError as exc:
            raise _parameter_error(key, str(exc)) from None
        if v < p["min"] or v > p["max"]:
            raise _parameter_error(key, f"{key}={v} out of range [{p['min']}, {p['max']}]")
        if ptype == "int":
            if not v.is_integer():
                raise _parameter_error(key, f"{key} must be a whole number")
            cleaned[key] = int(v)
        else:
            cleaned[key] = v
        if "options" in p and cleaned[key] not in {option["value"] for option in p["options"]}:
            raise _parameter_error(key, f"{key} must be one of {[option['value'] for option in p['options']]}")

    errors = []
    if primitive == "tube" and not cleaned["R_IN"] < cleaned["R_OUT"]:
        _validation_errors(errors, ("R_IN", "R_OUT"),
                           "R_IN must be smaller than R_OUT")
    if primitive == "bracket" and cleaned["D"] >= min(cleaned["L"] / 2, cleaned["W"]):
        _validation_errors(errors, ("D",),
                           "Hole diameter D too large for plate size")
    if primitive == "gridfinity_bin":
        if cleaned["REFINED"] and cleaned["MAGNETS"]:
            _validation_errors(errors, ("REFINED", "MAGNETS"),
                               "refined holes exclude magnet holes (original rule)")

        height = _gridfinity_bin_height(cleaned)
        lip_support = 1.2 if cleaned["LIP"] else 0.0
        max_fill = height - 7.0 - lip_support
        fill = cleaned["FILL"] if cleaned["FILL"] > 0 else max(0.0, max_fill)
        if cleaned["FILL"] > 0 and cleaned["FILL"] > max_fill:
            _validation_errors(
                errors, ("FILL", "HU", "HMODE", "LIP"),
                f"fill must be at most {max(0.0, max_fill):g} mm for this height and lip; "
                "reduce fill, increase height, or disable the lip")

        width = cleaned["GX"] * 42.0 - 0.5 - 2 * cleaned["WALL"]
        depth = cleaned["GY"] * 42.0 - 0.5 - 2 * cleaned["WALL"]
        if width <= 0 or depth <= 0:
            _validation_errors(errors, ("WALL", "GX", "GY"),
                               "wall thickness leaves no interior; reduce wall or increase grid size")
        if cleaned["DX"] > 0 and cleaned["DY"] > 0:
            if cleaned["DEPTH"] > 0 and cleaned["DEPTH"] > fill:
                _validation_errors(
                    errors, ("DEPTH", "FILL"),
                    f"compartment depth must be at most {fill:g} mm so it does not remove the base wall")
            if fill > 0:
                cell_width = width / cleaned["DX"]
                cell_depth = depth / cleaned["DY"]
                max_opening = min(cell_width, cell_depth) - 0.6
                if cleaned["CYL"] and cleaned["CD"] + 2 * cleaned["CCHAM"] > max_opening:
                    _validation_errors(
                        errors, ("CD", "CCHAM"),
                        f"cylindrical opening including chamfer must fit within {max_opening:g} mm; "
                        "reduce diameter/chamfer or use fewer divisions")
            elif cleaned["DEPTH"] > 0:
                _validation_errors(
                    errors, ("DEPTH", "FILL", "HU", "HMODE", "LIP"),
                    "compartment depth needs a positive fill height; increase height or set fill to auto")

    if primitive == "gridfinity_baseplate":
        if cleaned["REFINED"] and cleaned["MAGNETS"]:
            _validation_errors(errors, ("REFINED", "MAGNETS"),
                               "refined holes exclude magnet holes (original rule)")
        if cleaned["MAGNETS"] and cleaned["T"] < 4.6:
            _validation_errors(errors, ("MAGNETS", "T"),
                               "Thickness >= 4.6mm needed for magnet holes")
        if (cleaned["SCREW"] and (cleaned["CHAMFER"] or cleaned["HOLESTYLE"] in (1, 2))
                and cleaned["SCREW_HEAD"] < cleaned["SCREW_D"]):
            _validation_errors(
                errors, ("SCREW_HEAD", "SCREW_D"),
                "screw head diameter must be at least the screw diameter for this hole style")

    if errors:
        raise ParameterValidationError(errors)
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


gridfinity_baseplate_SPEC = {'label': 'Gridfinity Baseplate', 'blurb': 'Grid the bins snap into, with sockets + magnet holes.', 'params': [{'key': 'GX', 'label': 'Grid X', 'unit': 'u', 'ptype': 'int', 'default': 4, 'min': 1.0, 'max': 6.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': 'Grid width in 42 mm cells.'}}, {'key': 'GY', 'label': 'Grid Y', 'unit': 'u', 'ptype': 'int', 'default': 4, 'min': 1.0, 'max': 6.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': 'Grid depth in 42 mm cells.'}}, {'key': 'T', 'label': 'Thickness', 'unit': 'mm', 'ptype': 'number', 'default': 5, 'min': 4.6, 'max': 8.0, 'step': 0.2, 'ui': {'group': 'Size', 'help': 'Baseplate thickness.'}}, {'key': 'DISTX', 'label': 'Minimum X', 'unit': 'mm', 'ptype': 'number', 'default': 0, 'min': 0.0, 'max': 300.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': '0 means automatic grid width; increase to fit a larger plate.'}}, {'key': 'DISTY', 'label': 'Minimum Y', 'unit': 'mm', 'ptype': 'number', 'default': 0, 'min': 0.0, 'max': 300.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': '0 means automatic grid depth; increase to fit a larger plate.'}}, {'key': 'FITX', 'label': 'Fit X', 'unit': '', 'ptype': 'number', 'default': 0, 'min': -1.0, 'max': 1.0, 'step': 0.1, 'ui': {'group': 'Size', 'help': '0 centers the grid; -1 and 1 move it to an edge.'}}, {'key': 'FITY', 'label': 'Fit Y', 'unit': '', 'ptype': 'number', 'default': 0, 'min': -1.0, 'max': 1.0, 'step': 0.1, 'ui': {'group': 'Size', 'help': '0 centers the grid; -1 and 1 move it to an edge.'}}, {'key': 'STYLE', 'label': 'Plate style', 'unit': '', 'ptype': 'int', 'default': 0, 'min': 0.0, 'max': 4.0, 'step': 1.0, 'options': [{'value': 0, 'label': 'Plain'}, {'value': 1, 'label': 'Weighted'}, {'value': 2, 'label': 'Skeletonized'}, {'value': 3, 'label': 'Screw-together'}, {'value': 4, 'label': 'Screw-together minimal'}], 'ui': {'group': 'Advanced', 'help': 'Changes plate structure and adds weight or screw-together features.'}}, {'key': 'HOLESTYLE', 'label': 'Mount holes', 'unit': '', 'ptype': 'int', 'default': 0, 'min': 0.0, 'max': 2.0, 'step': 1.0, 'options': [{'value': 0, 'label': 'Plain'}, {'value': 1, 'label': 'Countersunk'}, {'value': 2, 'label': 'Counterbored'}], 'ui': {'group': 'Mounting', 'help': 'Select the screw-hole head shape.'}}, {'key': 'SCREW_D', 'label': 'Screw diameter', 'unit': 'mm', 'ptype': 'number', 'default': 3.35, 'min': 2.0, 'max': 5.0, 'step': 0.05, 'ui': {'group': 'Mounting', 'help': 'Diameter of screw-together and screw holes.'}}, {'key': 'SCREW_HEAD', 'label': 'Screw head diameter', 'unit': 'mm', 'ptype': 'number', 'default': 5, 'min': 3.0, 'max': 8.0, 'step': 0.1, 'ui': {'group': 'Mounting', 'help': 'Head diameter for screw holes.'}}, {'key': 'SCREW_SPACING', 'label': 'Screw spacing', 'unit': 'mm', 'ptype': 'number', 'default': 0.5, 'min': 0.0, 'max': 2.0, 'step': 0.1, 'ui': {'group': 'Mounting', 'help': 'Extra spacing between screw-together holes.'}}, {'key': 'NSCREWS', 'label': 'Screws per seam', 'unit': '', 'ptype': 'int', 'default': 1, 'min': 1.0, 'max': 3.0, 'step': 1.0, 'ui': {'group': 'Mounting', 'help': 'Number of screws on each plate seam.'}}, {'key': 'SOCKETS', 'label': 'Bin sockets', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Mounting', 'help': 'Cut sockets for Gridfinity bins.'}}, {'key': 'REFINED', 'label': 'Refined holes', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Mounting', 'help': 'Refined and magnet holes are mutually exclusive.', 'exclusiveWith': 'MAGNETS'}}, {'key': 'MAGNETS', 'label': 'Magnet holes (6x2)', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Mounting', 'help': 'Refined and magnet holes are mutually exclusive.', 'exclusiveWith': 'REFINED'}}, {'key': 'SCREW', 'label': 'Screw holes (M3)', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Mounting', 'help': 'Add screw holes through the plate.'}}, {'key': 'CRUSH', 'label': 'Crush ribs', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Advanced', 'help': 'Only applies to magnet holes.', 'dependsOn': 'MAGNETS'}}, {'key': 'CHAMFER', 'label': 'Hole chamfer', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Advanced', 'help': 'Chamfer enabled mounting holes.'}}, {'key': 'PRINTABLE', 'label': 'Supportless hole tops', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Advanced', 'help': 'Bridge hole tops for supportless printing.'}}, {'key': 'CORNERS', 'label': 'Holes only at corners', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Advanced', 'help': 'Use only the outer corner hole positions.'}}]}


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
# pi-lens-ignore: no-star-imports
import math

from build123d import (
    Align, Box, Cone, Cylinder, Plane, Polyline, Pos, RectangleRounded, Rot,
    Sketch, extrude, loft, make_face,
)

GX = 4  # spec: int label=Grid X unit=u min=1 max=6 step=1 group=Size help=Grid width in 42 mm cells.
GY = 4  # spec: int label=Grid Y unit=u min=1 max=6 step=1 group=Size help=Grid depth in 42 mm cells.
T = 5  # spec: number label=Thickness unit=mm min=4.6 max=8 step=0.2 group=Size help=Baseplate thickness.
DISTX = 0  # spec: number label=Minimum X unit=mm min=0 max=300 step=1 group=Size help=0 means automatic grid width; increase to fit a larger plate.
DISTY = 0  # spec: number label=Minimum Y unit=mm min=0 max=300 step=1 group=Size help=0 means automatic grid depth; increase to fit a larger plate.
FITX = 0  # spec: number label=Fit X min=-1 max=1 step=0.1 group=Size help=0 centers the grid; -1 and 1 move it to an edge.
FITY = 0  # spec: number label=Fit Y min=-1 max=1 step=0.1 group=Size help=0 centers the grid; -1 and 1 move it to an edge.
# FITX: -1=left, 0=center, 1=right; FITY: -1=bottom, 0=center, 1=top.
STYLE = 0  # spec: int label=Plate style options=0:Plain|1:Weighted|2:Skeletonized|3:Screw-together|4:Screw-together minimal min=0 max=4 step=1 group=Advanced help=Changes plate structure and adds weight or screw-together features.
HOLESTYLE = 0  # spec: int label=Mount holes options=0:Plain|1:Countersunk|2:Counterbored min=0 max=2 step=1 group=Mounting help=Select the screw-hole head shape.
SCREW_D = 3.35  # spec: number label=Screw diameter unit=mm min=2 max=5 step=0.05 group=Mounting help=Diameter of screw-together and screw holes.
SCREW_HEAD = 5  # spec: number label=Screw head diameter unit=mm min=3 max=8 step=0.1 group=Mounting help=Head diameter for screw holes.
SCREW_SPACING = 0.5  # spec: number label=Screw spacing unit=mm min=0 max=2 step=0.1 group=Mounting help=Extra spacing between screw-together holes.
NSCREWS = 1  # spec: int label=Screws per seam min=1 max=3 step=1 group=Mounting help=Number of screws on each plate seam.
SOCKETS = True  # spec: bool label=Bin sockets group=Mounting help=Cut sockets for Gridfinity bins.
REFINED = False  # spec: bool label=Refined holes group=Mounting exclusiveWith=MAGNETS help=Refined and magnet holes are mutually exclusive.
MAGNETS = True  # spec: bool label=Magnet holes (6x2) group=Mounting exclusiveWith=REFINED help=Refined and magnet holes are mutually exclusive.
SCREW = False  # spec: bool label=Screw holes (M3) group=Mounting help=Add screw holes through the plate.
CRUSH = True  # spec: bool label=Crush ribs group=Advanced dependsOn=MAGNETS help=Only applies to magnet holes.
CHAMFER = True  # spec: bool label=Hole chamfer group=Advanced help=Chamfer enabled mounting holes.
PRINTABLE = False  # spec: bool label=Supportless hole tops group=Advanced help=Bridge hole tops for supportless printing.
CORNERS = False  # spec: bool label=Holes only at corners group=Advanced help=Use only the outer corner hole positions.

# Slab footprint: cells tile at the 42mm pitch (BASEPLATE_DIMENSIONS, gridfinity-baseplate.scad:19).
_W0, _D0 = GX * 42.0, GY * 42.0
W, D = max(_W0, DISTX), max(_D0, DISTY)
# Shift the cell grid within the slab; FIT=0 keeps equal margins.
_PX = (W - _W0) * FITX / 2
_PY = (D - _D0) * FITY / 2
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
            _sock = loft(Sketch() + [Pos(_cx, _cy, _PT - _sockD) * (Plane.XY * RectangleRounded(36.3, 36.3, 1.15)), Pos(_cx, _cy, 0) * (Plane.XY.offset(_PT + 0.5) * RectangleRounded(40.5, 40.5, 2.5))], ruled=True)  # pyright: ignore[reportArgumentType] -- build123d accepts this runtime profile compound
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
    for _x, _rot_y in ((-W / 2, 90), (W / 2, -90)):
        for _i in range(max(1, NSCREWS)):
            _y = (_i - (max(1, NSCREWS) - 1) / 2) * (SCREW_HEAD + SCREW_SPACING)
            result -= Pos(_x, _y, _PT / 2) * Rot(0, _rot_y, 0) * Cylinder(_r, 4.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
    for _y, _rot_x in ((-D / 2, -90), (D / 2, 90)):
        for _i in range(max(1, NSCREWS)):
            _x = (_i - (max(1, NSCREWS) - 1) / 2) * (SCREW_HEAD + SCREW_SPACING)
            result -= Pos(_x, _y, _PT / 2) * Rot(_rot_x, 0, 0) * Cylinder(_r, 4.0, align=(Align.CENTER, Align.CENTER, Align.MIN))
"""


gridfinity_bin_SPEC = {'label': 'Gridfinity Bin', 'blurb': 'Full Rebuilt port: compartments, tabs, scoop, holes, lip, height modes.', 'params': [{'key': 'GX', 'label': 'Grid X', 'unit': 'u', 'ptype': 'int', 'default': 2, 'min': 1.0, 'max': 6.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': 'Grid width in 42 mm cells.'}}, {'key': 'GY', 'label': 'Grid Y', 'unit': 'u', 'ptype': 'int', 'default': 2, 'min': 1.0, 'max': 6.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': 'Grid depth in 42 mm cells.'}}, {'key': 'HU', 'label': 'Height value', 'unit': '', 'ptype': 'int', 'default': 6, 'min': 0.0, 'max': 200.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': 'Value interpreted by Height mode.'}}, {'key': 'HMODE', 'label': 'Height mode', 'unit': '', 'ptype': 'int', 'default': 0, 'min': 0.0, 'max': 3.0, 'step': 1.0, 'options': [{'value': 0, 'label': 'Grid units'}, {'value': 1, 'label': 'Interior height'}, {'value': 2, 'label': 'Exterior height'}, {'value': 3, 'label': 'Exterior height with lip'}], 'ui': {'group': 'Size', 'help': 'Choose how Height value is interpreted.'}}, {'key': 'ZS', 'label': 'Snap height to 7mm', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Size', 'help': 'Round height up to the next 7 mm grid unit.'}}, {'key': 'FILL', 'label': 'Solid fill mm (0=auto)', 'unit': 'mm', 'ptype': 'number', 'default': 0, 'min': 0.0, 'max': 200.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': '0 means automatic fill depth; set a value to override it.'}}, {'key': 'WALL', 'label': 'Outer wall', 'unit': 'mm', 'ptype': 'number', 'default': 0.95, 'min': 0.95, 'max': 2.4, 'step': 0.05, 'ui': {'group': 'Size', 'help': 'Thickness of the outer wall.'}}, {'key': 'DX', 'label': 'Divisions X (0=solid)', 'unit': '', 'ptype': 'int', 'default': 1, 'min': 0.0, 'max': 6.0, 'step': 1.0, 'ui': {'group': 'Compartments', 'help': '0 means solid; positive values divide the interior along X.'}}, {'key': 'DY', 'label': 'Divisions Y (0=solid)', 'unit': '', 'ptype': 'int', 'default': 1, 'min': 0.0, 'max': 6.0, 'step': 1.0, 'ui': {'group': 'Compartments', 'help': '0 means solid; positive values divide the interior along Y.'}}, {'key': 'DEPTH', 'label': 'Compartment depth mm (0=full)', 'unit': 'mm', 'ptype': 'number', 'default': 0, 'min': 0.0, 'max': 200.0, 'step': 1.0, 'ui': {'group': 'Compartments', 'help': '0 means full fill depth; set a value to stop compartments above the base.'}}, {'key': 'SCOOPW', 'label': 'Scoop amount', 'unit': '', 'ptype': 'number', 'default': 1.0, 'min': 0.0, 'max': 1.0, 'step': 0.1, 'ui': {'group': 'Compartments', 'help': '0 means no scoop; 1 is the full scoop ramp.'}}, {'key': 'TABSTYLE', 'label': 'Tab style', 'unit': '', 'ptype': 'int', 'default': 1, 'min': 0.0, 'max': 5.0, 'step': 1.0, 'options': [{'value': 0, 'label': 'Full'}, {'value': 1, 'label': 'Auto'}, {'value': 2, 'label': 'Left'}, {'value': 3, 'label': 'Center'}, {'value': 4, 'label': 'Right'}, {'value': 5, 'label': 'None'}], 'ui': {'group': 'Labels', 'help': 'Controls label tabs on compartment walls.'}}, {'key': 'TABPLACE', 'label': 'Tab placement', 'unit': '', 'ptype': 'int', 'default': 0, 'min': 0.0, 'max': 1.0, 'step': 1.0, 'options': [{'value': 0, 'label': 'Every cell'}, {'value': 1, 'label': 'Top-left only'}], 'ui': {'group': 'Labels', 'help': 'Choose which compartments receive label tabs.'}}, {'key': 'CYL', 'label': 'Cylindrical compartments', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Compartments', 'help': 'Use cylindrical compartments instead of rounded rectangles.'}}, {'key': 'CD', 'label': 'Cylinder dia', 'unit': 'mm', 'ptype': 'number', 'default': 10, 'min': 1.0, 'max': 60.0, 'step': 0.5, 'ui': {'group': 'Compartments', 'help': 'Used only when cylindrical compartments is enabled.', 'dependsOn': 'CYL'}}, {'key': 'CCHAM', 'label': 'Cylinder top chamfer', 'unit': 'mm', 'ptype': 'number', 'default': 0.5, 'min': 0.0, 'max': 5.0, 'step': 0.1, 'ui': {'group': 'Compartments', 'help': 'Used only when cylindrical compartments is enabled.', 'dependsOn': 'CYL'}}, {'key': 'REFINED', 'label': 'Refined holes', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Mounting', 'help': 'Refined and magnet holes are mutually exclusive.', 'exclusiveWith': 'MAGNETS'}}, {'key': 'MAGNETS', 'label': 'Magnet holes (6x2)', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Mounting', 'help': 'Refined and magnet holes are mutually exclusive.', 'exclusiveWith': 'REFINED'}}, {'key': 'SCREW', 'label': 'Screw holes (M3)', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Mounting', 'help': 'Add screw holes through the bin base.'}}, {'key': 'THUMB', 'label': 'Thumbscrew holes', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Mounting', 'help': 'Add thumbscrew access holes.'}}, {'key': 'CRUSH', 'label': 'Crush ribs', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Advanced', 'help': 'Only applies to magnet holes.', 'dependsOn': 'MAGNETS'}}, {'key': 'CHAMFER', 'label': 'Hole chamfer', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Advanced', 'help': 'Chamfer enabled mounting holes.'}}, {'key': 'PRINTABLE', 'label': 'Supportless hole tops', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Advanced', 'help': 'Bridge hole tops for supportless printing.'}}, {'key': 'CORNERS', 'label': 'Holes only at corners', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Advanced', 'help': 'Use only the outer corner hole positions.'}}, {'key': 'LIP', 'label': 'Stacking lip', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Advanced', 'help': 'Add the stacking lip around the top.'}}], 'warnings': ['Thumbscrew threads are represented as plain holes; position is exact.', 'M3 screw threads are not modeled; holes are clearance holes.']}


gridfinity_bin_TEMPLATE = r"""
# object: Gridfinity Bin
# blurb: Full Rebuilt port: compartments, tabs, scoop, holes, lip, height modes.
# approximation: Thumbscrew threads are represented as plain holes; position is exact.
# approximation: M3 screw threads are not modeled; holes are clearance holes.
# Faithful port of kennetek/gridfinity-rebuilt-openscad
# (gridfinity-rebuilt-bins.scad). Construction mirrors the original CSG tree:
# tapered feet + bridge + base holes, wall ring, infill solid, per-compartment
# rounded cutters (minus scoop/tab solids) or cylinders, stacking lip ring.
# Parameter variables carry `# spec:` comments; packaging/bundle.py extracts
# the UI spec from them. Run standalone with build123d installed, or use
# through the orcad tab.
import math

from build123d import Align, Box, Cone, Cylinder, Plane, Polyline, Pos, RectangleRounded, Rot, Sketch, extrude, fillet, loft, make_face

GX = 2  # spec: int label=Grid X unit=u min=1 max=6 step=1 group=Size help=Grid width in 42 mm cells.
GY = 2  # spec: int label=Grid Y unit=u min=1 max=6 step=1 group=Size help=Grid depth in 42 mm cells.
HU = 6  # spec: int label=Height value min=0 max=200 step=1 group=Size help=Value interpreted by Height mode.
HMODE = 0  # spec: int label=Height mode options=0:Grid units|1:Interior height|2:Exterior height|3:Exterior height with lip min=0 max=3 step=1 group=Size help=Choose how Height value is interpreted.
ZS = False  # spec: bool label=Snap height to 7mm group=Size help=Round height up to the next 7 mm grid unit.
FILL = 0  # spec: number label=Solid fill mm (0=auto) unit=mm min=0 max=200 step=1 group=Size help=0 means automatic fill depth; set a value to override it.
WALL = 0.95  # spec: number label=Outer wall unit=mm min=0.95 max=2.4 step=0.05 group=Size help=Thickness of the outer wall.
DX = 1  # spec: int label=Divisions X (0=solid) min=0 max=6 step=1 group=Compartments help=0 means solid; positive values divide the interior along X.
DY = 1  # spec: int label=Divisions Y (0=solid) min=0 max=6 step=1 group=Compartments help=0 means solid; positive values divide the interior along Y.
DEPTH = 0  # spec: number label=Compartment depth mm (0=full) unit=mm min=0 max=200 step=1 group=Compartments help=0 means full fill depth; set a value to stop compartments above the base.
SCOOPW = 1.0  # spec: number label=Scoop amount min=0 max=1 step=0.1 group=Compartments help=0 means no scoop; 1 is the full scoop ramp.
TABSTYLE = 1  # spec: int label=Tab style options=0:Full|1:Auto|2:Left|3:Center|4:Right|5:None min=0 max=5 step=1 group=Labels help=Controls label tabs on compartment walls.
TABPLACE = 0  # spec: int label=Tab placement options=0:Every cell|1:Top-left only min=0 max=1 step=1 group=Labels help=Choose which compartments receive label tabs.
CYL = False  # spec: bool label=Cylindrical compartments group=Compartments help=Use cylindrical compartments instead of rounded rectangles.
CD = 10  # spec: number label=Cylinder dia unit=mm min=1 max=60 step=0.5 group=Compartments dependsOn=CYL help=Used only when cylindrical compartments is enabled.
CCHAM = 0.5  # spec: number label=Cylinder top chamfer unit=mm min=0 max=5 step=0.1 group=Compartments dependsOn=CYL help=Used only when cylindrical compartments is enabled.
REFINED = True  # spec: bool label=Refined holes group=Mounting exclusiveWith=MAGNETS help=Refined and magnet holes are mutually exclusive.
MAGNETS = False  # spec: bool label=Magnet holes (6x2) group=Mounting exclusiveWith=REFINED help=Refined and magnet holes are mutually exclusive.
SCREW = False  # spec: bool label=Screw holes (M3) group=Mounting help=Add screw holes through the bin base.
THUMB = False  # spec: bool label=Thumbscrew holes group=Mounting help=Add thumbscrew access holes.
CRUSH = True  # spec: bool label=Crush ribs group=Advanced dependsOn=MAGNETS help=Only applies to magnet holes.
CHAMFER = True  # spec: bool label=Hole chamfer group=Advanced help=Chamfer enabled mounting holes.
PRINTABLE = True  # spec: bool label=Supportless hole tops group=Advanced help=Bridge hole tops for supportless printing.
CORNERS = False  # spec: bool label=Holes only at corners group=Advanced help=Use only the outer corner hole positions.
LIP = True  # spec: bool label=Stacking lip group=Advanced help=Add the stacking lip around the top.

# ---- height (gridfinity-rebuilt-utility.scad: height() + z_snap) ----
_Hraw = HU * 7.0 if HMODE == 0 else (HU + 7.0 if HMODE == 1 else (HU if HMODE == 2 else HU - 4.4))
if ZS:
    _Hraw = _Hraw if _Hraw % 7 == 0 else _Hraw + 7 - _Hraw % 7
H = max(_Hraw, 7.0)
W = GX * 42.0 - 0.5
D = GY * 42.0 - 0.5
_EW = WALL
_inner_w = W - 2 * _EW
_inner_d = D - 2 * _EW
_inner_r = max(0.01, 3.75 - _EW)
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
        _foot = loft(Sketch() + _secs, ruled=True)  # pyright: ignore[reportArgumentType]
        _feet = _foot if _feet is None else _feet + _foot
result = _feet
# ---- bridge slab tying the feet together ----
result += Pos(0, 0, 4.75) * extrude(Plane.XY * RectangleRounded(W, D, 3.75), amount=2.25)  # pyright: ignore[reportOperatorIssue]
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
    _wall = extrude(Plane.XY * RectangleRounded(W, D, 3.75), amount=H - 7.0) - Pos(0, 0, -0.5) * extrude(Plane.XY * RectangleRounded(_inner_w, _inner_d, _inner_r), amount=H - 7.0 + 1)
    result += Pos(0, 0, 7.0) * _wall
if _fill > 0:
    # Keep infill inside the requested outer wall; otherwise it would hide
    # thicker walls when the compartment cutters are applied.
    result += Pos(0, 0, 7.0) * extrude(Plane.XY * RectangleRounded(_inner_w, _inner_d, _inner_r), amount=_fill)
# ---- compartments: per-division rounded cutters (element minus 0.6 total),
# minus scoop/tab solids; cylinders replace cutters when CYL ----
if DX > 0 and DY > 0 and _fill > 0:
    # Compartments tile the requested interior; the 0.6mm subtraction leaves
    # the fixed divider/edge web inside the wall.
    _rx = _inner_w / DX
    _ry = _inner_d / DY
    _ztop = _infill_top + 0.02
    _dep = DEPTH if DEPTH > 0 else _fill
    for _ix in range(DX):
        for _iy in range(DY):
            _cx = -_inner_w / 2 + (_ix + 0.5) * _rx
            _cy = -_inner_d / 2 + (_iy + 0.5) * _ry
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
    _lip_outer = loft(Sketch() + _lo + [Plane.XY.offset(H + 3.55) * RectangleRounded(W - 1.1, D - 1.1, 3.0)], ruled=True)  # pyright: ignore[reportArgumentType]
    _lip_inner = loft(Sketch() + _li, ruled=True)  # pyright: ignore[reportArgumentType]
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


def _reserve_export_path(out_dir, stem, ext):
    """Reserve a unique final path, even when exports start in one second."""
    base = stamped_filename(stem, ext)
    candidate = out_dir / base
    suffix = 0
    while True:
        try:
            fd = os.open(candidate, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        except FileExistsError:
            suffix += 1
            candidate = out_dir / f"{Path(base).stem}_{suffix}.{ext}"
        else:
            os.close(fd)
            return candidate


def _remove_export_file(path):
    with suppress(FileNotFoundError):
        path.unlink()


# ---------------------------------------------------------------------------
# Runner (build123d imported lazily so tests + plugin load stay light)
# ---------------------------------------------------------------------------


class _CadCancelled(RuntimeError):
    """Internal marker: a running CAD result was requested to be discarded."""


_CAD_HOOKS = threading.local()


@contextmanager
def _cad_hooks(progress=None, cancelled=None):
    previous = getattr(_CAD_HOOKS, "value", None)
    _CAD_HOOKS.value = (progress, cancelled)
    try:
        yield
    finally:
        _CAD_HOOKS.value = previous


def _cad_hook_values(progress, cancelled):
    hooked = getattr(_CAD_HOOKS, "value", None)
    if hooked:
        progress = progress or hooked[0]
        cancelled = cancelled or hooked[1]
    return progress, cancelled


def _cad_check_cancelled(cancelled):
    if cancelled and cancelled():
        raise _CadCancelled("CAD cancellation requested")


def _cad_stage(progress, stage, message):
    if progress:
        progress(stage, message)


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
    """Return named, unit-bearing shape measurements plus legacy raw values."""
    stats = {}
    for attr in ("volume", "area"):
        with suppress(Exception):
            value = cast(Any, getattr(shape, attr, None))
            numeric = round(_num(value() if callable(value) else value, attr), 3)
            # Keep the original keys for protocol consumers; the suffixed keys
            # make the unit and dimensionality unambiguous for new consumers.
            stats[attr + "_mm"] = numeric
            stats[attr + ("_mm3" if attr == "volume" else "_mm2")] = numeric
    with suppress(Exception):
        bb = shape.bounding_box()
        stats["bbox_min"] = [round(_num(x, "bbox"), 3) for x in bb.min]
        stats["bbox_max"] = [round(_num(x, "bbox"), 3) for x in bb.max]
        size = [round(_num(x, "bbox"), 3) for x in bb.size]
        stats["bbox_size"] = size
        if len(size) >= 3:
            width, depth, height = size[:3]
            stats["width_mm"] = width
            stats["depth_mm"] = depth
            stats["height_mm"] = height
            stats["dimensions_mm"] = {
                "width": width, "depth": depth, "height": height,
            }
    return stats


def _preview_payload(shape, tolerance=DEFAULT_TOLERANCE):
    """Complete triangle soup for the page's 3D preview. Best-effort: None on failure.

    Dropping triangles by stride makes parameter changes look stale when the
    changed surfaces are among the omitted triangles.
    """
    try:
        vertices, triangles = shape.tessellate(_num(tolerance, "tolerance"), 0.1)
        total = len(triangles)
        if total == 0:
            return None
        flat = []
        for tri in triangles:
            for idx in (int(tri[0]), int(tri[1]), int(tri[2])):
                v = vertices[idx]
                flat.extend([round(float(v.X), 3), round(float(v.Y), 3), round(float(v.Z), 3)])
        return {"tris": flat, "shown": total, "total": total}
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


def preview_shape(code, tolerance=DEFAULT_TOLERANCE, progress=None, cancelled=None):
    """Execute code, return stats + preview payload WITHOUT exporting.

    Returns dict (JSON-able). Raises RuntimeError on failure. CAD cancellation
    is cooperative: native build/tessellation calls are allowed to finish.
    """
    progress, cancelled = _cad_hook_values(progress, cancelled)
    tol = _tolerance(tolerance)
    if len(code) > 200_000:
        raise ValueError("code too large (>200k chars)")
    _cad_check_cancelled(cancelled)
    _cad_stage(progress, "building", "Building preview…")
    shape, var = _execute_code(code)
    _cad_check_cancelled(cancelled)
    _cad_stage(progress, "tessellating", "Tessellating preview…")
    preview = _preview_payload(shape, tol)
    _cad_check_cancelled(cancelled)
    return {"ok": True, "var": var, "stats": _shape_stats(shape),
            "preview": preview}


def _open_with_default_app(path):
    """Send *path* to the OS opener; return once the request is handed off.

    A successful process launch is not proof that OrcaSlicer imported the file.
    Spawning the opener is ProcessCreate-audited: the user approves it once per
    plugin (remembered in .install_state.json).
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
    return True


def _remember_plate_export(capability, code, tolerance, stem, result):
    """Remember only the latest successful plate STL, not a general export cache."""
    path = Path(result["file"])
    try:
        stat = path.stat()
    except OSError:
        return
    capability._orcad_last_plate_export = {
        "code": code,
        "format": "stl",
        "tolerance": tolerance,
        "stem": stem,
        "result": dict(result),
        "size_bytes": stat.st_size,
        "mtime_ns": stat.st_mtime_ns,
    }


def _reuse_plate_export(capability, code, tolerance, stem):
    """Return a still-valid matching plate export, or None."""
    cached = getattr(capability, "_orcad_last_plate_export", None)
    if not isinstance(cached, dict) or any(
            cached.get(key) != value for key, value in (
                ("code", code), ("format", "stl"),
                ("tolerance", tolerance), ("stem", stem))):
        return None
    result = cached.get("result")
    if not isinstance(result, dict) or result.get("format") != "stl":
        return None
    try:
        path = Path(result["file"])
        stat = path.stat()
    except (KeyError, OSError, TypeError, ValueError):
        return None
    if (path.suffix.lower() != ".stl" or not path.is_file() or stat.st_size <= 0
            or stat.st_size != cached.get("size_bytes")
            or stat.st_size != result.get("size_bytes")
            or stat.st_mtime_ns != cached.get("mtime_ns")):
        return None
    reused = dict(result)
    reused["size_bytes"] = stat.st_size
    return reused


def run_build123d_code(code, export_format="stl", tolerance=DEFAULT_TOLERANCE,
                       filename_stem="model", progress=None, cancelled=None):
    """Execute user code, export result. Returns dict (JSON-able). No orca needed.

    Raises RuntimeError with user-facing message on failure. Cancellation is
    cooperative because build123d and its native CAD calls cannot be safely
    hard-stopped from this plugin's worker thread.
    """
    progress, cancelled = _cad_hook_values(progress, cancelled)
    if export_format not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of {EXPORT_FORMATS}")
    tol = _tolerance(tolerance)
    if not (0.0001 <= tol <= 1.0):
        raise ValueError("tolerance must be within [0.0001, 1.0]")
    if len(code) > 200_000:
        raise ValueError("code too large (>200k chars)")

    _cad_check_cancelled(cancelled)
    _cad_stage(progress, "building", "Building model…")
    shape, var = _execute_code(code)
    _cad_check_cancelled(cancelled)

    out_dir = exports_dir()
    out_path = _reserve_export_path(out_dir, filename_stem, export_format)
    temp_path = None
    installed = False
    try:
        temp_fd, temp_name = tempfile.mkstemp(
            prefix=f".{out_path.stem}.", suffix=f".{export_format}", dir=out_dir)
        temp_path = Path(temp_name)
        os.close(temp_fd)
        if export_format in ("stl", "3mf"):
            _cad_stage(progress, "tessellating", f"Tessellating {export_format.upper()}…")
        _cad_stage(progress, "exporting", f"Exporting {export_format.upper()}…")
        _cad_check_cancelled(cancelled)
        if export_format == "stl":
            from build123d import export_stl
            ok = export_stl(shape, str(temp_path), tolerance=tol, angular_tolerance=0.1)
            if not ok:
                raise RuntimeError("export_stl reported failure")
        elif export_format == "step":
            from build123d import export_step
            ok = export_step(shape, str(temp_path))
            if not ok:
                raise RuntimeError("export_step reported failure")
        else:  # 3mf via Mesher
            from build123d import Mesher
            m = Mesher()
            m.add_shape(shape, linear_deflection=tol, angular_deflection=0.1)
            m.write(str(temp_path))

        if not temp_path.is_file():
            raise RuntimeError(f"Export to {export_format} produced no output")
        size = temp_path.stat().st_size
        if size == 0:
            raise RuntimeError(f"Export to {export_format} produced empty output")
        os.replace(temp_path, out_path)
        installed = True
    except RuntimeError:
        raise
    except Exception as exc:
        raise RuntimeError(f"Export to {export_format} failed: {exc}") from exc
    finally:
        if not installed:
            if temp_path is not None:
                _remove_export_file(temp_path)
            _remove_export_file(out_path)

    _cad_check_cancelled(cancelled)
    _cad_stage(progress, "tessellating", "Tessellating preview…")
    preview = _preview_payload(shape, tol)
    _cad_check_cancelled(cancelled)
    filename = out_path.name
    return {
        "ok": True,
        "file": str(out_path),
        "filename": filename,
        "format": export_format,
        "var": var,
        "size_bytes": size,
        "stats": _shape_stats(shape),
        "preview": preview,
    }


# ---------------------------------------------------------------------------
# Page UI — compiled local Vue/Tailwind app with bundled Three.js.
# Prefer the self-contained asset beside this entry point; the embedded copy
# keeps the single-file plugin installable.
# ---------------------------------------------------------------------------

# BEGIN BUNDLED FRONTEND
_EMBEDDED_FRONTEND_GZIP = b"""\n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\n4YUIL0R4IcKbIrwpwpsivCnCmyK8KcKbIrwpwpsivCnCmyK8KcKbIrwpwpsivOl0/5ylPjeu/urkwIW9/yHs04ftAxGlzzoX\nWc3RSnCYmvrLSQJnyHqvMwKubNgZ9T+3WXhbYn5bYnRLotSw+0d+tvym0++fNzD0+PGh+eEOzK9Oj3/tN/cxJlRpoSomk6io\n2/886MkMhcL5N4PuZ7dzSMl5sXxuVZ3r0vAV6ZDZ2sgqEqkiji2uok954bLwfMYN4vvXwA0o2/g5swwATGaWhbzlf4SN0PkP\n46Yi822gNbeZO/VlI68bmf4R6PNlbfnNsklyEiglgYbYdL4ffeMcSk1aJVe1Dm/dNE5gy6ZgnKCdAo5gz190LDNOwjCT8DeL\nr5SAb+kaT6/C9+wzOKvn3Q5dzY6ldCT3IPtLtCylHGgk0pncQSHToN/vGumHhWSYGJ54UqpAzQLkcnvD3iGcA4YMhkgVKFRm\nFOh2hgM77+GurG6HZ5TWA/J5mlzt0TNtmqLhsZPlRz8G5kndvOzRjfW+eZX1z/wvHl2+XJM1Ktgaq9XZOdJWARp0Z9eoW1lh\nApxbZ0BlVHNw+wTI/DQFt46/zAkz8HXDzw84Sf7JGO1aiNcqaH4kyY33hSj7lS5qOmhZlFjlay/kgY2Xb2foUgKlLvRtH8+0\nzVRKKI5MUJDyc8GMQoENFRBJssQGSmRSJc8Cb7SBAltSyvFjH21S7Moaqqxvd+XCxHfFRLPkd3I0eaMk3gpcbY8FtNx0I+2O\nZVsLbqNLc0Ynnb0I5mad41U9PVuO9/aBRm6NFuEwGg2paIbVN9kKq09f14gtN1+jhcChGMEzMYnXKkYdr3tXm4rxVjkguOHj\ni6+NxYwNI2fDyrpjvhtqwmUIM3MszipRCcrkVmvg064mFOUrilP76rnZwrrEyfVlqXVNoztN3R3IuqM7TdWdpkLD9eXt3Wna\n3Wmq7ohXm/J01I1m1XWzZP4dbaurtskQnMTREkYp54EB/0DD57mroRfqdg8A/fwVRAANTKPrbs14zhAYJw7zG7KSG/I7anWz\nIYYHXzDreSM9657XI/gZnMuRSs9cntLjKcNzMQjRsoy+mr/hVUP3HAnITttA2sYRoJA3qgYFKTtByTQTFNDwxapq/jUTBrB0\nU+TwXztl4DgfRs6NXLpmVXKSbqvQ5Pp2V1bKZVf0nO5NapZmGWXjFyo1x5pQC4Oiz59dp4g41RWjCOeWc4CV/eFJohNWH4wU\n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\n4Z8FmAQUNbllSnvK8sm/kjSenJB1vNApZhGA1JWvs10v7we8WHJXDUYjDPsH30cpL/yXLZxeYQGYC8dAmNLCMdGkfIf3RX4U\nWzcHml5+qXbdv0dD59+khfM/UtPmf5j+zP8+zZi/V//l71dz+Xcps/x5lZUv1FH5M4opf4Uqyl+qfPK/Rtfk36FUUu3hkyJl\nzfLq3YosXJ7zu3N+dU4XxhCDF+cbuje/Kfr2pXvtRDv085uRvC6/8fjNt28xMi+X8kxmvjhrrD2quvnW71L6nlu8PeKjwIF9\n4ysvJgszHDm6Da9if2nu9YFvCDK3yk0VUrJwFgxIBGAaV7BNPHlX9Z268fbnOP8/dTCqOD1E3YK30UZaT/8qj6NFWkIrRb5m\nn67mYRrBtOxwUKXSv4N9D59kW6k/idawdlqreYSy3vMw98vgZHa+wK1nWe4ssx6W1o8h/WHvluRrs64u4dJ6UcRDVv/dJo6W\nk9v6Y+Qo94j6wqSX1h3ZK0lW3XQDKo81ikTVTZehZU+Jr5IsKp4KdrL+OR4Q1V0mngNlNTme9irAC+9MBXy0sghnjiEQFbV9\nSFytzugaGdPbMnacaqbu2stLA5VWtF4BKuxFxpuC4SCv3nO2lX4q71u+6xQkAMu704VxX3NhXNZc3Ni4iFpFVeVTQ39Pl778\nk3qPf4mm41+g2/h3aTP+HcqLAqY+luvXupqVu9lCdwDy0U7Gqme4jvV65zbDer1TxPaoRAQQ88t0Qz0G/AkdSunYK+COvR6y\nf3HnUh8zYR/uuqhRQNngSN5m4n98oeSRTYxRfxylbvBtYvj9hQOXp+zQwtKWzzOFG4BoqR7AZV7BbBSEBKWnQ5Hrffh0sUIJ\nHWVWn78dSV7gKH8UHeUNryscul2vYIq+3bxKomVeu1i2rLXhWFMjqyyh7D0qJwFJUXfjlqqrlkNlI6hU9uUdr6wcl2xlLd+R\nSdYny4nU69NM78VSzkeRZ4MVcLRjLonbi4pTipdik0lxh+baYlyw2i1gh+4ts7qjy/xFVq912zFYajgGDe+Gok0lCEKUCIMb\nHbzxcKkoiDITBDc6COdamWsbZZUVYNlHui6hVElwdPTGiL7R0Tf0VEezXOy6qu04NE4S47BlPDhJOKoXVciAVXD8uWcFxnuO\nhKtoh7MtoG55tPlNoR5YfqUnnV7zwvo4VSzNn650af6YZ5fecWsJpfUbXrMARJWxQRWwPGxxy/F0HyzeJR64DJKMI9wsTBZh\n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\njMe8jH9DeLfb2K/XmvioZBlY02kfdFhKi91Up7zkQ3iTtHeNnCPZ2KS/iqWnZcIhxiAUgqdPNlHLlUASGXixuGkAAWufMgwg\nBgS89BT7V4SwUSlEehTyGMEmhcDa8hMiUcXNLXqyvbS5oVQMwlfeImK1VdoiR12KT58WNYDixjqTuxL5+3Tr8Salio7T89mO\nf+ZNB7BgH4F5FU9q43EUMn2TmSlDmtykwal7xkftDA3lGeqOWVl2EmiQx+Ss1GyD5ICwrWU2gGkbIg2SwHouFbY2SRKB2Cw8\n3XxKCggIGPzlraccCX6XSlsllk1ZXV4vy9F//v7Gw/78fWMU+xPV5tMG/lMYpeUnpcfr66XHW0/X6ah+sl56ugW6CYjaeEKS\noI+3npZL5S34v8fGiAetBTkboOaelh4/JRriySYklLa2YKCVH7MefbL5ZGtro/R0s7he2ipa1Mbj0mZxswQ65jGjo1jaWAfI\nzY3NJ483yf2Op9C5wIrNp+tbm08fb9m0yvl7GFfpLQZxheYWi4+flp6WNjYAFZP0DZT6J5vrG9DmdSTAklrWdQphDDRxc3Nj\nvbgFg2y9TAcEzADl9SfFcrkEvC07Fsjy1oaJCkV+a31r/emT0hPQr4+frDu29PLjJ1gHcGsLOFPcfLz1uAi811lBjZLa+Wwv\nGNUvyP20Urmw8QTQVBMg3oyAbIDKBwq3FC21eBCbWm/p9G4IY1JrkvmffEAT/bUSueDJM+lLYUlVmhNGgmgvnfdF29QcpSCz\n7lh92kVQDpKuNSwq3ZC8ZPOIzZJm4yQNELMbUrR9YvJgn0xZbE/jSThcoC8yWrHM9SfnSLfgtdh0D6yh6ND+Vj6rKWAkYp3Y\nQpAgPJgeouG/VQBvMvFHUw+93TvyErQlNaUQD4pnJhnLOqWd9LlYe1v1t7PULLq6auspJHhkZH8AVI84aOOIUZkSdhBrUj6P\n2Mtdohoa1S8k8ezMLSFmxuqxcIkbVGyhrZGyMmr+4o3x0YLdcMYh4H0vw580fRsOpkO/6ZNrPYgBY1Y7eMR64SOuagBDJZkv\nItPSX24VHeLeRlwYKImSqJLrJKIiKXgCDavoBEftMlO21EdmLeKbXzZG6F2HVAhHZWvBc+ygpJpJOPEG/BFXhlhN01GRl/5S\nkfFFdGORijCWxrpiMDKV4a7n2Ee3En90/dbDOel9MAbvjQapBYsWDFSg0GKlqjW1aEl2QGcxVQxWj75KXHmKIJtZpihXzbDf\n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\n"""
# END BUNDLED FRONTEND

PAGE_ASSET = Path(__file__).with_name("frontend") / "dist" / "index.html"


def _load_page_html():
    if PAGE_ASSET.is_file():
        return PAGE_ASSET.read_text(encoding="utf-8")
    try:
        return gzip.decompress(base64.b64decode(_EMBEDDED_FRONTEND_GZIP)).decode("utf-8")
    except Exception as exc:
        raise RuntimeError(f"Missing compiled frontend: {PAGE_ASSET}") from exc


PAGE_HTML = _load_page_html()


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


def _warnings_for_msg(msg):
    """Return source metadata warnings for predefined objects, never for raw code."""
    if not isinstance(msg, dict) or msg.get("kind") != "generate":
        return []
    spec = PRIMITIVES.get(str(msg.get("primitive", "")))
    return list(spec.get("warnings", ())) if spec else []


class _CadJob:
    def __init__(self, identity, work, dedupe_key=None):
        self.identity = identity
        self.work = work
        self.dedupe_key = dedupe_key
        self.cancelled = threading.Event()


class _CadJobScheduler:
    """Run one CAD job at a time, coalescing previews and canceling by identity."""

    def __init__(self):
        self._condition = threading.Condition()
        self._preview = None
        self._exports = []
        self._pending_keys = set()
        self._active_job = None
        self._worker = None

    def _start_worker_locked(self):
        if self._worker is None or not self._worker.is_alive():
            self._worker = threading.Thread(
                target=self._run, name="orcad-cad", daemon=True)
            self._worker.start()

    def submit_preview(self, work, identity=None):
        with self._condition:
            if self._preview is not None:
                self._preview.cancelled.set()
            self._preview = _CadJob(identity, work)
            self._start_worker_locked()
            self._condition.notify()

    def submit_export(self, key, work, identity=None):
        with self._condition:
            if key in self._pending_keys or (
                    self._active_job is not None
                    and self._active_job.dedupe_key == key):
                return False
            job = _CadJob(identity if identity is not None else key, work, key)
            self._exports.append(job)
            self._pending_keys.add(key)
            self._start_worker_locked()
            self._condition.notify()
            return True

    def is_cancelled(self, identity):
        with self._condition:
            if self._active_job is not None and self._active_job.identity == identity:
                return self._active_job.cancelled.is_set()
            if self._preview is not None and self._preview.identity == identity:
                return self._preview.cancelled.is_set()
            return any(job.identity == identity and job.cancelled.is_set()
                       for job in self._exports)

    def cancel(self, identity):
        """Cancel exactly one request; return pending, running, or None."""
        with self._condition:
            if self._active_job is not None and self._active_job.identity == identity:
                self._active_job.cancelled.set()
                return "running"
            if self._preview is not None and self._preview.identity == identity:
                self._preview.cancelled.set()
                self._preview = None
                self._condition.notify()
                return "pending"
            for index, job in enumerate(self._exports):
                if job.identity == identity:
                    self._exports.pop(index)
                    self._pending_keys.discard(job.dedupe_key)
                    job.cancelled.set()
                    self._condition.notify()
                    return "pending"
        return None

    def _run(self):
        while True:
            with self._condition:
                while self._preview is None and not self._exports:
                    self._condition.wait()
                if self._exports:
                    job = self._exports.pop(0)
                    self._pending_keys.remove(job.dedupe_key)
                else:
                    job = self._preview
                    self._preview = None
                    if job is None:
                        continue
                self._active_job = job
            try:
                job.work()
            finally:
                with self._condition:
                    self._active_job = None


def _cad_scheduler(capability):
    scheduler = getattr(capability, "_orcad_cad_scheduler", None)
    if scheduler is None:
        scheduler = _CadJobScheduler()
        capability._orcad_cad_scheduler = scheduler
    return scheduler


def _message_context(capability, msg):
    """Return IDs echoed on every request/response message."""
    if not isinstance(msg, dict):
        return {"request_id": None, "revision_id": None}
    request_id = msg.get("request_id")
    if request_id is None:
        request_id = getattr(capability, "_orcad_request_id", 0) + 1
        capability._orcad_request_id = request_id
    return {
        "request_id": request_id,
        "revision_id": msg.get("revision_id", msg.get("revision", 0)),
    }


def _error_payload(exc):
    payload = {"error": str(exc)}
    if isinstance(exc, ParameterValidationError):
        payload["errors"] = list(exc.errors)
    return payload


def _handle_message_sync(capability, msg):
    """Route one JS message; CAD work is serialized by one worker."""
    context = _message_context(capability, msg)
    if not isinstance(msg, dict):
        return {"type": "error", "ok": False, "error": "message must be an object", **context}
    cmd = msg.get("command")
    if cmd == "ping":
        return {"type": "pong", "ok": True, **context}
    if cmd == "code":
        # Editor mirror: pure codegen, no CAD run, no thread, instant.
        try:
            code, _stem = _build_code_from_msg(msg, str(msg.get("kind", "generate")))
        except Exception as exc:
            return {"type": "code", "ok": False, **_error_payload(exc), **context}
        return {"type": "code", "ok": True, "code": code, **context}
    if cmd == "cancel":
        target_type = str(msg.get("target_type", "operation"))
        target_request = msg.get("target_request_id")
        target_revision = msg.get("target_revision_id", msg.get("revision_id"))
        target_seq = msg.get("target_seq")
        identity = ("preview", target_request, target_revision, target_seq) \
            if target_type == "preview" else ("export", target_request, target_revision)
        state = _cad_scheduler(capability).cancel(identity)
        if state == "pending":
            message = "cancelled"
        elif state == "running":
            message = "Cancellation requested; running CAD work will finish and its result will be discarded."
        else:
            message = "operation already finished or was superseded"
        cancelled = {"type": "cancelled", "ok": False, "cancelled": state is not None,
                     "pending": state == "pending", "message": message,
                     "request_id": target_request, "revision_id": target_revision}
        if target_type == "preview":
            cancelled["seq"] = target_seq
        return cancelled
    if cmd in ("run", "generate"):
        try:
            export_format = str(msg.get("format", "stl")).lower()
            tolerance = float(msg.get("tolerance", DEFAULT_TOLERANCE))
            code, stem = _build_code_from_msg(msg, cmd)
            warnings = _warnings_for_msg(msg)
        except Exception as exc:
            return {"type": "error", "ok": False, **_error_payload(exc), **context}

        scheduler = _cad_scheduler(capability)
        identity = ("export", context["request_id"], context["revision_id"])

        def _work():
            def is_cancelled():
                return scheduler.is_cancelled(identity)

            def progress(stage, message):
                if not is_cancelled():
                    capability.post_message({"type": "progress", "stage": stage,
                                             "message": message, **context})

            try:
                with _cad_hooks(progress, is_cancelled):
                    res = run_build123d_code(code, export_format, tolerance, stem)
                if is_cancelled():
                    return
                capability.post_message({
                    "type": "result", "ok": True, "export_ok": True,
                    "file": res["file"], "filename": res["filename"],
                    "format": res["format"], "var": res["var"],
                    "size_bytes": res["size_bytes"], "stats": res["stats"],
                    "warnings": warnings, "preview": res.get("preview"), **context,
                })
            except _CadCancelled:
                return
            except Exception as exc:
                if not is_cancelled():
                    capability.post_message({"type": "error", "ok": False,
                                             "export_ok": False, "open_request_sent": False,
                                             "error": str(exc)[:4000], **context})

        key = ("export", code, export_format, tolerance, stem)
        if not scheduler.submit_export(key, _work, identity):
            return {"type": "progress", "stage": "duplicate", "message": "Already running or queued…",
                    "duplicate": True, **context}
        return {"type": "progress", "stage": "queued", "message": "Queued…", **context}
    if cmd == "preview":
        # Live preview: run CAD, post mesh, export nothing. Stale requests
        # (slider moved again while building) are dropped via the seq guard.
        seq = msg.get("seq", context["request_id"])
        preview_context = {**context, "seq": seq}
        try:
            tolerance = float(msg.get("tolerance", DEFAULT_TOLERANCE))
            code, _stem = _build_code_from_msg(msg, str(msg.get("kind", "generate")))
            warnings = _warnings_for_msg(msg)
        except Exception as exc:
            return {"type": "preview", "ok": False, **_error_payload(exc), **preview_context}
        scheduler = _cad_scheduler(capability)
        identity = ("preview", context["request_id"], context["revision_id"], seq)
        capability._orcad_preview_token = identity

        def _work():
            def is_cancelled():
                return scheduler.is_cancelled(identity)

            def progress(stage, message):
                if not is_cancelled() and getattr(capability, "_orcad_preview_token", None) == identity:
                    capability.post_message({"type": "progress", "stage": stage,
                                             "message": message, **preview_context})

            try:
                with _cad_hooks(progress, is_cancelled):
                    res = preview_shape(code, tolerance)
                if is_cancelled() or getattr(capability, "_orcad_preview_token", None) != identity:
                    return
                capability.post_message({"type": "preview", "ok": True,
                                         "var": res["var"], "stats": res["stats"],
                                         "warnings": warnings, "preview": res.get("preview"), **preview_context})
            except _CadCancelled:
                return
            except Exception as exc:
                if (is_cancelled()
                        or getattr(capability, "_orcad_preview_token", None) != identity):
                    return
                capability.post_message({"type": "preview", "ok": False,
                                         "error": str(exc)[:2000], **preview_context})

        scheduler.submit_preview(_work, identity)
        return None
    if cmd == "open_exports":
        try:
            folder = exports_dir()
            _open_with_default_app(folder)
            return {"type": "folder_result", "ok": True,
                    "folder": str(folder), "message": "open request sent", **context}
        except Exception as exc:
            return {"type": "folder_result", "ok": False,
                    "error": str(exc)[:4000], **context}
    if cmd == "plate":
        # Send to plate has separate export and OS-open states. The host does
        # not report whether its single-instance import actually completed.
        try:
            tolerance = float(msg.get("tolerance", DEFAULT_TOLERANCE))
            code, stem = _build_code_from_msg(msg, str(msg.get("kind", "generate")))
            warnings = _warnings_for_msg(msg)
        except Exception as exc:
            return {"type": "plate_result", "ok": False, "export_ok": False,
                    "open_request_sent": False, "handoff_status": "export_failed",
                    **_error_payload(exc), **context}

        scheduler = _cad_scheduler(capability)
        identity = ("export", context["request_id"], context["revision_id"])

        def _work():
            def is_cancelled():
                return scheduler.is_cancelled(identity)

            def progress(stage, message):
                if not is_cancelled():
                    capability.post_message({"type": "progress", "stage": stage,
                                             "message": message, **context})

            try:
                res = _reuse_plate_export(capability, code, tolerance, stem)
                reused = res is not None
                if reused:
                    progress("exporting", "Using existing STL export…")
                else:
                    with _cad_hooks(progress, is_cancelled):
                        res = run_build123d_code(code, "stl", tolerance, stem)
                if is_cancelled():
                    return
                _remember_plate_export(capability, code, tolerance, stem, res)
            except _CadCancelled:
                return
            except Exception as exc:
                if not is_cancelled():
                    capability.post_message({"type": "plate_result", "ok": False,
                                             "export_ok": False, "open_request_sent": False,
                                             "handoff_status": "export_failed",
                                             "message": "export failed",
                                             "error": str(exc)[:4000], **context})
                return

            exported = {
                "type": "plate_result", "ok": False, "export_ok": True,
                "open_request_sent": False, "handoff_status": "open_request_failed",
                "file": res["file"], "filename": res["filename"],
                "size_bytes": res["size_bytes"], "stats": res["stats"],
                "warnings": warnings, "preview": res.get("preview"),
                "message": "export succeeded; open request failed",
                "reused": reused, **context,
            }
            if is_cancelled():
                return
            progress("open-request", "Sending open request…")
            try:
                _open_with_default_app(res["file"])
            except Exception as exc:
                if not is_cancelled():
                    exported["open_request_error"] = str(exc)[:4000]
                    capability.post_message(exported)
            else:
                if not is_cancelled():
                    exported.update({"ok": True, "open_request_sent": True,
                                     "handoff_status": "open_request_sent",
                                     "message": "open request sent",
                                     "open_request_error": None})
                    capability.post_message(exported)

        key = ("plate", code, tolerance, stem)
        if not scheduler.submit_export(key, _work, identity):
            return {"type": "progress", "stage": "duplicate", "message": "Already running or queued…",
                    "duplicate": True, **context}
        return {"type": "progress", "stage": "queued", "message": "Queued…", **context}
    return {"type": "error", "ok": False, "error": f"unknown command {cmd!r}", **context}


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
                                           "error": f"handler failed: {exc}",
                                           **_message_context(self, msg)})

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
