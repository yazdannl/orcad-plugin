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


gridfinity_bin_SPEC = {'label': 'Gridfinity Bin', 'blurb': 'Full Rebuilt port: compartments, tabs, scoop, holes, lip, height modes.', 'params': [{'key': 'GX', 'label': 'Grid X', 'unit': 'u', 'ptype': 'int', 'default': 2, 'min': 1.0, 'max': 6.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': 'Grid width in 42 mm cells.'}}, {'key': 'GY', 'label': 'Grid Y', 'unit': 'u', 'ptype': 'int', 'default': 2, 'min': 1.0, 'max': 6.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': 'Grid depth in 42 mm cells.'}}, {'key': 'HU', 'label': 'Height value', 'unit': '', 'ptype': 'int', 'default': 6, 'min': 0.0, 'max': 200.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': 'Value interpreted by Height mode.'}}, {'key': 'HMODE', 'label': 'Height mode', 'unit': '', 'ptype': 'int', 'default': 0, 'min': 0.0, 'max': 3.0, 'step': 1.0, 'options': [{'value': 0, 'label': 'Grid units'}, {'value': 1, 'label': 'Interior height'}, {'value': 2, 'label': 'Exterior height'}, {'value': 3, 'label': 'Exterior height with lip'}], 'ui': {'group': 'Size', 'help': 'Choose how Height value is interpreted.'}}, {'key': 'ZS', 'label': 'Snap height to 7mm', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Size', 'help': 'Round height up to the next 7 mm grid unit.'}}, {'key': 'FILL', 'label': 'Solid fill mm (0=auto)', 'unit': 'mm', 'ptype': 'number', 'default': 0, 'min': 0.0, 'max': 200.0, 'step': 1.0, 'ui': {'group': 'Size', 'help': '0 means automatic fill depth; set a value to override it.'}}, {'key': 'WALL', 'label': 'Outer wall', 'unit': 'mm', 'ptype': 'number', 'default': 0.95, 'min': 0.95, 'max': 2.4, 'step': 0.05, 'ui': {'group': 'Size', 'help': 'Thickness of the outer wall.'}}, {'key': 'DX', 'label': 'Divisions X (0=solid)', 'unit': '', 'ptype': 'int', 'default': 1, 'min': 0.0, 'max': 6.0, 'step': 1.0, 'ui': {'group': 'Compartments', 'help': '0 means solid; positive values divide the interior along X.'}}, {'key': 'DY', 'label': 'Divisions Y (0=solid)', 'unit': '', 'ptype': 'int', 'default': 1, 'min': 0.0, 'max': 6.0, 'step': 1.0, 'ui': {'group': 'Compartments', 'help': '0 means solid; positive values divide the interior along Y.'}}, {'key': 'DEPTH', 'label': 'Compartment depth mm (0=full)', 'unit': 'mm', 'ptype': 'number', 'default': 0, 'min': 0.0, 'max': 200.0, 'step': 1.0, 'ui': {'group': 'Compartments', 'help': '0 means full fill depth; set a value to stop compartments above the base.'}}, {'key': 'SCOOPW', 'label': 'Scoop amount', 'unit': '', 'ptype': 'number', 'default': 1.0, 'min': 0.0, 'max': 1.0, 'step': 0.1, 'ui': {'group': 'Compartments', 'help': '0 means no scoop; 1 is the full scoop ramp.'}}, {'key': 'TABSTYLE', 'label': 'Tab style', 'unit': '', 'ptype': 'int', 'default': 1, 'min': 0.0, 'max': 5.0, 'step': 1.0, 'options': [{'value': 0, 'label': 'Full'}, {'value': 1, 'label': 'Auto'}, {'value': 2, 'label': 'Left'}, {'value': 3, 'label': 'Center'}, {'value': 4, 'label': 'Right'}, {'value': 5, 'label': 'None'}], 'ui': {'group': 'Labels', 'help': 'Controls label tabs on compartment walls.'}}, {'key': 'TABPLACE', 'label': 'Tab placement', 'unit': '', 'ptype': 'int', 'default': 0, 'min': 0.0, 'max': 1.0, 'step': 1.0, 'options': [{'value': 0, 'label': 'Every cell'}, {'value': 1, 'label': 'Top-left only'}], 'ui': {'group': 'Labels', 'help': 'Choose which compartments receive label tabs.'}}, {'key': 'CYL', 'label': 'Cylindrical compartments', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Compartments', 'help': 'Use cylindrical compartments instead of rounded rectangles.'}}, {'key': 'CD', 'label': 'Cylinder dia', 'unit': 'mm', 'ptype': 'number', 'default': 10, 'min': 1.0, 'max': 60.0, 'step': 0.5, 'ui': {'group': 'Compartments', 'help': 'Used only when cylindrical compartments is enabled.', 'dependsOn': 'CYL'}}, {'key': 'CCHAM', 'label': 'Cylinder top chamfer', 'unit': 'mm', 'ptype': 'number', 'default': 0.5, 'min': 0.0, 'max': 5.0, 'step': 0.1, 'ui': {'group': 'Compartments', 'help': 'Used only when cylindrical compartments is enabled.', 'dependsOn': 'CYL'}}, {'key': 'REFINED', 'label': 'Refined holes', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Mounting', 'help': 'Refined and magnet holes are mutually exclusive.', 'exclusiveWith': 'MAGNETS'}}, {'key': 'MAGNETS', 'label': 'Magnet holes (6x2)', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Mounting', 'help': 'Refined and magnet holes are mutually exclusive.', 'exclusiveWith': 'REFINED'}}, {'key': 'SCREW', 'label': 'Screw holes (M3)', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Mounting', 'help': 'Add screw holes through the bin base.'}}, {'key': 'THUMB', 'label': 'Thumbscrew holes', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Mounting', 'help': 'Add thumbscrew access holes.'}}, {'key': 'CRUSH', 'label': 'Crush ribs', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Advanced', 'help': 'Only applies to magnet holes.', 'dependsOn': 'MAGNETS'}}, {'key': 'CHAMFER', 'label': 'Hole chamfer', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Advanced', 'help': 'Chamfer enabled mounting holes.'}}, {'key': 'PRINTABLE', 'label': 'Supportless hole tops', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Advanced', 'help': 'Bridge hole tops for supportless printing.'}}, {'key': 'CORNERS', 'label': 'Holes only at corners', 'unit': '', 'ptype': 'bool', 'default': False, 'ui': {'group': 'Advanced', 'help': 'Use only the outer corner hole positions.'}}, {'key': 'LIP', 'label': 'Stacking lip', 'unit': '', 'ptype': 'bool', 'default': True, 'ui': {'group': 'Advanced', 'help': 'Add the stacking lip around the top.'}}]}


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
_EMBEDDED_FRONTEND_GZIP = b"""\n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\ntcgKy2lcteqUFWDLLKe5tBzpMI2U/vWKszMxfoyoQHN+kMBXFf6qLmUuDQX4jxzVRZ7djXjExtl7+8FUjxe4Cevr2UytLbpn\ns2/KDeqGnhou78Mahp8HriM0TzTuCBWUt/44FMoIxtkeZRpNzZizpcx1v33YHrtm2jN/rIQ3CyD+q+MIb7immDWmoNjfzRVv\nqHdXvAdXFT04NnvA9ScsQFflHlCuz+jBldGDK6sHV9iDq5srNjWBOJaT9dQrzyXhUBbedNav2Yd9k+Mo0HGHmDbJbWou559j\nTj3vbX74azT+ZUY9UFVD8/+5SkODI95siodA7oazfA6E9XJQedTzpPmQ15XadQyN+aSe63x8XaVl16vUsuOBa3UY89KtenfX\nFXp31zfp3V1U6d1daL077pPiRt07q2Wfq42HrZOFv4Z+3u/FxGsj8ddi4sXX1uyj2M5Y1GzFdseiyv8oLUDRYGvEZHutkfp3\n6QsKrBA48RnagzjTqhweZBTXCFT2Yux+roph67phAMTwxeerHhIQDeJvqiPyLsKn2U34vPj7yoqit/KL9/fzFRlb101j0Jp6\n0D5HwZGAaBB/U+mRD1rTHrSmGrTPVImEITI6WNcd/GxVSYKk4Ygp1FxX72YFR7FMhKzzBQqfblN59LnKo88VG32UvU3hx+2c\n1jM5Yj5XfvRRQBfzjESerspzDXk6PM+A53HbIlNPZbqATF2eaSgyuSJTX2b6bK1PtIB4GWUPUQcKJTZ/Xvt41hCCdKLeTl1r\nLMGkOAdlPSnkjy7qlCirvO+Gzd6hoECeK0ejLbvcdsY1jWJ7uYFge7mBXnu5lr+32ipF/rQIKr1lc4HNAymraUiyB16Mgmtr\n+CG59BjFzObw46Jceszl0mOUy7+Enw7KpccooTbFLG0yW6WYznVz7txvtd1vv1VRs+ZlMeqouaIoOhbosg0saxVtXBZijhqr\nQkzQWDSmzS7GOlsUOPhAHeTSwosYhBceFHadvQ6beLUFDz32alMeeu1Rg/Z67I1HDYFQHHrUgr2e0ll7MIG2PHh8+AL7c1jj\nzFZr2B667cFw34XVoj9gZiMDOV6gJtHrvQjdde9FzngCYCYSjAGCm7K1wKQGmAlKcb7eQ/dkcbiXOuPHlTCsNiGSZQaMxwjj\nzR5qNQCMTPCsJKlLYr4S1xCTjo1ytVXzyKnT3wZNdJ3+Nmrz5hrC+Nc5KCrLHePcIz7Ujj15lX7pEZC9Y4nuBEd+XnsESn5e\n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\nbnCUPklaj588ffj2+fHZqzePn7xRipD0NnpXl+n8rSf93+UumjpVBEORBIonK3B8CO7mPtozinyWK2kxzv+4O2nesu0upc9M\nn9JnplNpG4T2Av1FLmdZbo4UYKz2Op2aTy4Zf9/w+ftGwp83Yv6sEfBnjTV/1Zjxx4w5f8xY8McM6VY6L7uVFkYTfTzkfIPS\nyNwmrvZXmjj3WyP+n3a+aHh0a67ZQnu4MhNQjs7wPmokzVmgi7SdrR6pddOa0LZ1qW3rctuuzHqSLU2Do5humVWiOWPZtqZZ\nzqytUZuXWja/pWVQz9ZRs9rWrm4/yktW+8G+KozarNS22S0zOt86atZ8trc3f4sL7Wt71OJSy+LbcS3Q/tWK01bpGbeADc4W\n79vXhVHzS23zbx216qYlW1eBuXKuNK7d5Lib9izts7tATm533p07mzKxBzIcfY4rb1M+wLp9eB20bpQlsExfFNr+OiBQsg5x\nk1akkNWiaCF56rhm1Du1a4mczwtukeiYabXEaOPzAF/Pv4YfRCkcYm6qM9PEackXYtv0hWh5QuzwhyZDYLZmbUGQ5vy7XSPy\nDSv8D/Q8yMd6U+T7uOSZ4Bi/mVkco7K7mr33XM7HmFE19/798FPbefDgQXsTLv1JHFoZPnk8g0h7GMcWxKYLw39+Xij0L1lo\nGmUliN96/6+sVKYXobY3eWiqltZ04ZB+UM2gjcYnqFHTypyyEp53g+ecnxdw0OEsyONAsN5pKhhu9p3i5F9LBv2V5PLDc6lE\nfC6zvw0Ue0/s/rNAsf8ufr8MjOMAsP/Lc+8jipaPd/3pNJzublikYtJwkXzAODGHz/Kd8DIPl9Ns5yItTGe2XiFnK007cWuK\n3cfI7fNwC6hstAxfpwlkzPnlE9uNprvsI5yF1+H450WjsZFWHdfR1POliYHW0l+E3u4u/8C2ebuyAhG58lNg20jJQ+irCxuQ\ntPoI5Mp7lisEffu6JVjaA/n8YapjTxIWyRFNrfOStoaDTydRgdKhpdGNyuHXSIG4RPMixqkK5c71uiV3scan71SOXSTFjj5K\ny6RjnIxZdL5OEfHGMObhcr0I1Rcf4HDDUrEF3K1AvmF/qVbfrUi0YfQCdrfc6YYtkmkY/xKFF3xXGgtk4Ci/YUvDw5yZ9iMc\neZVdJkyUi8SIIjOhFfEP13nylszEmhjx4uHxm2e/nT18e/wKsOPxw+MnJVg3Fvz11Zvnj28s/jIMp5kof094pI/9qzDl6/ob\n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\n5jHEL1L3bmlonEiGvwM4LUqOejfOGeIr7cS9MXWnciUxrXzxHfz+4sNYhb+tMB8PwuArOUVXo6vRbXpgi0xlGXpJ37z5Rhes\nBJDGvcb7b0oie5Kc5pHY5HLVgO+PY/U5351gMAIjqxWSrLeQRWP2kRWpbNcUWtkVcfxUWkU8GXKgwb2kYd48HuYtZoGYRKwC\nN5CMCSpfWAg3Eb3AlauQF0ORjafjPmBoC/JlRDcQ6bjN2Am9HSZddBOQRumzhu7L24t5ZgoNmHfzoFoWpOrJAbM+G7wSsiro\n0ShUY6dDf0U9DBOVr3Z6CVTv+vw2bo8tPJwu+zVypvTXuOc87KJP/w3592M3uX4preed3yzpG3nnd0vyWl5q4kaYuAxzXCTW\nlf8cg+n6IoB8hvv9QtdbilmIuYfd45BAR+rTPlGyzjDvEFg3yjseCx0rgq1GjmJHddySE+CtNow/UV1e7uSDZTd2xsedE6VA\nIDRXpLyiGFM/ConAL+xB8VgOe6VR3jimUaI4aDV+7ldjsH6w+HGMBzs8+COL8VcelddhWIU6Qs9EiNgILoXUgJakpd50j72T\nqv48EuHh7lhwEB+lRTA3lrzCVwl8xic/H+OW+7ggopbQAQ/Te6xERVHeBGVeJ+3OdrfTHXjVGGQA0ZKn89g52hILKgLzY+jg\nWESDVX3IbpCOj7iEZnKHJ8crKMyurmKwKslvfLlO1lfW6itgLN0LXit8YSlSpspPc0Hvx1A/d1IriM51yjcpoGK2TL6bdPxd\npr+/ac1YtTTDQaBkUxxEQRCIbQmvENqqDpWqz5SqT+dVjf+dYXAGUXXIqz7jVZ8uZufqYnaOfig78fcq/f1Va99aCmsxjtI8\n9jqIj2Bb2Ni1xY09S20sUQXsmAdxi8pZPD+vUmoZ6WILc6ZSu0a2LzD83iWGHYYQXIDr9y5D5qtNad9TU1Zv35Tf0pqSwPWb\ntSnn99SUtds35WNaUxK4PlqbMrulnvYX6OnmD9bT/o/S0/7CsVa/Rz3t31VPz1WWi9tU+4uUpX//ynJxYy9uKcvTRcJ8+oOF\nefrDpHm6mButexTn6f3I8w1atfkXCfT0L5DoGzT3a2IbVplrQgx8ZLXHfdryMFV1w4oJYdzQiBsb0yd9xwX6VFCjXd4mj2gd\nvHvR6VWCXpWEQ2jxy4U57+oq0uIfNO6dYEclYZWQ0O1pNGz9tTSQG3xAxVSn4vCvpaK8zVZyV1dDnZA9cl6EHeUYswMcT0rl\n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\nGlNGIyvorr5uPrNCbW5vp7Ourtn1ttUpOQzHY+QokVqr3JRU31heKYOdXnrsMW5fwmLHG74jV7dKj3PKa7Tq28t58hht3iHA\ntNKK7wBU3Z9UYvUI6iwZ9YkYzFvBGJdVGAnDjQvsWZsqdSXGBTX+uxO4GL9WDf8u9gxgqMa4M3BO/k7dGfnbd5vk78Ctk78j\nQCCiPWazlSFJbbpd8nfmwkopyjsTN2TOV7ZgybEwQE7LqQOGs2E0fEuDZMGCjYXLwtWRmrMMiyJYm1WxwkbnKyWXP4L3Itdz\nj8W6CkdxrUHGC8FCF5s41B0TZn+zrsLs4ybMzL7+c/iLavh2e66HKg8SW+7xyLnvmltqzS1ac76CbYb5BBj9w+p34rlraYbh\nqFkjRiXY583W5v5WjbgQ4sJpB0Y22GU65MvN1tZrGHsMiN0LTYDt7hNcNkDifE6DhsXM3kGDlZlG3k5whvdQ+4GBf+fglQ2o\nNpuUJSAAtWvvD8sMctSZ6EhAl7Asqhv0XKpsUgCyy1OaMWTrM70sWSnI0p0gAbF5ILOZh8xg9FH9UIIIz5kOtH/QqG/uWcAO\nSMRS4l9LKdE+ePkbTCDNw03R41b3XFpx6LBXtX2tfI848rvkXkeCVJAuWLFs1epQSOEr8T2de0nu13ebzd23NYWJPMrwhcHJ\n/d3mQatxcPghAZjkuoCVeIXvMQG9tVfbbGwdbLYswI1gejYAVRyll2o3Do5evYY1WtNSft/eobKw2bV+6Pc8fKi6m2TWbmN3\nu9bcqsHgtRZoDfzuFyuxSsn2/2fv3dvaSHKH0b93P4XDm5PXPVQ8QDLzm22nh8fcEk/AOLYJEzgctrHbl2C7Pb6BB/juR1Ld\nVN1tA5ns5Vye3QnuKpXqplKpVCqp8aG8+9Ft7mQUNdE5eqpcvbq/e3JYqqVBaXkvhZfrO10Kj6nAp6aL5SXRw0WlXm6wqRsv\nnYSMoR9E07CfCXy03ygdusBhf9QN0+RzWP1QSgB9CCfdDKgPpfoHBcZ1rC5ko1aq1InQQQxLA6fq5/C2GdOls5s1o104hC6Z\now/7+5XUBCH8cmKXZbJGOxydzC0scoyTz7RDUY47yhxSj7EGtzAuw+VlNLvVZSwM479OJce8hpiBMobECzB2pIs5kA4j5wUV\nG9eFDESC4/IilseqQgwqk9Hywgk2q1GkSqTXhDNdbEWYeXNg0wuQl+cEocu7sBms2pkgzqjNRLnQafbNMXDmrRG4sMs4ciaW\n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\ntbOcpAyxCCK3IH1nSnEZvsQ8d+emL63gNt6dDsfZFDyc5KqivrFiFdJpaF2vf720U0Vl8bQdhurwTWz3AcgKuPvdxuwcp2c7\noxEGGrYa+m47eg1S8EM7h5skb0ynEzIwM2F6V9IpOi5vPS+nB60XCL0LObm/R+HIzb4cBOQHhVcmVFe4o/W7jtKvDiUHSc1z\nabn/JNqLK0uitPjIoDQ3v9PIzc1vg+ZI1sYpPOf65zJs59k0G/+5+Ju7BXgqwUVwNTn17Wfxn9tfy/YNz6a5e4arX+mNw83v\n9Bbi5nfeTuLud2ZbqblZSUu2F0+nLNtrPJu2ZPfxTFLOduTOZ87G5M5naoty/dPZqOraTv5m5emU9Obl5vfSXcxzEpdsbJ5J\nWrbHeTYtu9+5/cVc+UfIB5Ir9vvLBGCYVr2a4YwkdqekYErLY3AmXqPm0wv9DsT8aQnyceISWosVOsv0DcKPEirEuK0OQeHc\n43c4cVUkdKUNt8uyEDy2Uw76Rmqx8uvpCrozs+/oolluQyqz7hSVNQMM0ZJbjXLqppCm6r3gPr8fzKmbQrLCmOM76BQ4NltH\nZdadoiqCrrp5kK0nsupWMVvcVLj6zkhw/X4aV1mocJXDT4Wue65DbwpxU7kShdVlDIFz+0l5p/l269PBcefTB9muHVhWbQ17\nfSX6J/4zUdxGBYnoVpKvNqw4ZwvUW7/TqXzh91L+kDrtx7u0dCvQWtxLktSqa4k+gvA7whDQjamWQ7sgpLeF/1z9R82i6Xay\nlMjWq6sM/vPzxFT4ophBdgoCTk1DPz5t4v+IUdkxgdQT0jLio3wWWSfC1NBbEu+qZ2TEjnJXE9frq6nUuH5uvcKM1S4W1ptw\nVCltsnRaXJArLA0rXU6OdmHhC8XEN2TJWhkW1i3obEynS+7gcffWQ5LC1+nUB4RYWle80JJuYhBFX7eS4uUUP/LCJneCbLCq\nJBt9OMmNPuwAY2kk4g7QiZDC4pTXyWmD/ph/6I/8VcHVDfWQsK69aJYgm1rsc36xUK7LEmOyi2byLG6lcFmG+sXrlOZJIxvR\nTQH8MoXSIrazKeUPpexbe5TTH2dbcvnD2YqKcMhPSTRke3MrjrzkcZalS1TFdF9xToWhvD5fOIYCHMwKnd0UxcjqOtlPUxNd\nziIwqSK+PTGp6Kvp1a0snxWdyd5BWSA4Dw9O6r1I9akRRaKcEkkkzmPIRiSBby2E4jorVV+FlBPB8T29QgLBmbY78rZmSinC\n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\n"""
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
                    "preview": res.get("preview"), **context,
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
                                         "preview": res.get("preview"), **preview_context})
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
                "preview": res.get("preview"),
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
