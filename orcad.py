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
            "build123d/OCP is not ready in Orca's Python environment. On first run, "
            "bundled `uv` may download hundreds of MB and needs network and write "
            "access; this can take time. Reopen the Plugins dialog or restart "
            "OrcaSlicer to retry dependency setup, then run again. If it still "
            "fails, check network/write permissions and Orca's Python log. "
            f"({exc})"
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
_EMBEDDED_FRONTEND_GZIP = b"""\n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\n/cLKmuATFowtW5Hi2Q0QuyydXeyyScRDTRR59dNddhwrqYnmqrvL3ibmt8q2SPD2j4fPFbQZxKZhMGyPIBbCCCBIveFgv82+\nD7w+dD2ewU+HBfjTZWv86bH1BH76bIpfAzbBnyGb488+W82o3Hc4he1ej/0SeLvQyZ0w22U/IJp3YYiAkED3PyRICdwDuhjf\n+T69rXprc27zeZS1zmMRl3nGkb+YdLtx1NVeIe0gOiWrkqosfaNwPgaUpEvuCCMoFEtS+8CiAh9S0TCk5lvaVr5pLDZHXmpC\nNcYlRqEtxJ9strwpPqQB+i3IzieeDtNHRS+4GW3Bsv6DTHBIqTzRZgxZI/uqMPyElkTlltClwZmUvJDNCcX1FuVX3Yjke58W\nveC2XFJlfOeJTwZ40IINP6YhAGTfFURu3lrsgt/l3uluG3bB3baLfzr4p4t/evinj38G+GeIf/bxzwj/+PjnAv8E+GeCf0L8\nM4U/LsJzEZ6L8FyE5yI8F+G5CM9FeC7CcxGei/BchOciPBfhuQjPRXgdhNdBeB2E10F4HYTXQXgdhNdBeB2E10F4HYTXQXgd\nhNdBeB2E10F4XYTXRXhdhNdFeF2E10V4XYTXRXhdhNdFeF2E10V4XYTXRXhdhNdFeD2E10N4PYTXQ3g9hNdDeD2E10N4PYTX\nQ3g9hNdDeD2E10N4PYTXQ3h9hNdHeH2E10d4fYTXR3h9hNdHeH2E10d4fYTXR3h9hNdHeH2E10d4yOXsDhDeAOENEN4A4Q0Q\n3gDhDRDeAOENEN4A4Q0Q3gDhDRDeAOENEN4Q4Q0R3hDhDRHeEOENEd4Q4Q0R3hDhDRHeEOENEd4Q4Q0R3hDhDRHePsLbR3j7\nCG8f4e0jvH2Et4/w9hHePsLbR3j7CG8f4e0jvH2Et4/w9hHeCOGNEN4I4Y0Q3gjhjRDeCOGNEN4I4Y0Q3gjhjRDeCOGNEN4I\n4Y0Qno/wfITnIzwf4fkIz0d4PsLzEZ6P8HyE5yM8H+H5CM9HeD7C8xHeBcK7QHgXCO8C4V0gvAuEd4HwLhDeBcK7QHgXCO8C\n4V0gvAuEd4HwLhBegPAChBcgvADhBQgvQHgBwgsQXoDwAoQXILwA4QUIL0B4AcILEN4E4U0Q3gThTRDeBOFNEN4E4U0Q3gTh\nTRDeBOFNEN4E4U0Q3gThTRBeiPBChBcivBDhhQgvRHghwgsRXojwQoQXIrwQ4YUIL0R4IcILEd4U4U0R3hThTRHeFOFNEd4U\n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\ng7lqfYjFDT/i/YNsbjJEpj40G7U6Ghp+Zv9j1xrNrS08h15KYSuf5+VFb4tuUyjFAvdLW2Z831PLbU5FWcmDuniiBilVGD0u\n3fRA9rtwI77Y/urBrSvsPbPF2KBpDmltE6Lc6BrvkXmdX2dn9pP6Gax5xIS/fGWYfiAiLe3NQDeaR7wpLLN9oXBzQMeovzef\nTQaD3Ox+3Ivu23fq7dYX4+gHxsdJ+/Xe/s9cejG5JR5zG2SF6Tbznkrpfe+xv2EXPHp5L5v8F57bpNel6m2xalnscuNSc68p\n41QdRsMhkFTetJlBLOPDajurCLYnqRGrIm+tRfXswtaktxZdV+VuqVl8qNYldQijq2iVQPYWesB0ppPZssECn7CIZtC5zlCY\nAtLoKo1ysqPx9VCgmvdWZEdEqdvkDHzx5pG9lD84vpDDiz2SF9q1mvvRUL3vc98Gmk8HF2Rz4eD3rQThFIAUjw4RmObGZp4J\nfTweTEhrPkidXUNY+vtQa0VPhloLpc8gBLZSTwUcjSLvwBrtweHFa0ntbGJBrGVYtc/Pjk+7vnZnId2sFp+S82ejwa2JRlJw\nJlk9QihP1U/RCwY7tB80ktUDXmoPq8/Le4WvYTw4MXO3JAlXpvom0ibq7ybqW1QQpC7sjekfque0Bqg5Rggz1ujoSQcAeSuG\nJeYIpb43ETuPsqCHk6AlivwuzHXFpAZa5/zMO85QUj00zPJn+oMD74kh74jzNS2WrOGpGcrkLzE4aRXo2FQY1tD0UPrGA1PR\n/dbjUkppeapn4QekZO9blcxcf/grEWdR0kRE/1C6pIf0qkEWYbsIYxV+xOyyphfliU0nuyAHZ/UJRxvNk6an2+Pnm3FLY9bI\n8tDn0B5EQ/bmnRoeq2l70d7vXu5dtPd8c/cCmnEvjSNr/qrJ6Z/N6PL4Jzx9NVKO2FNnay6SY8NZ77LWysmoExSTp5R+iWns\nVOljy671UfvNsa/CR2iyxwLRPqiLZL/f7xvtcJ1idN59j4piU70xvwXblKePcssDA8/C9u5PfCV45RHdmC073VcE/NMfs8VR\nyk+xsIrgb+k4NfnS27M5YgfvmjxpNVqqbN3AUxR1T0H3U8Sq/tB/nSub9PjVSf42z2iiZe+Sv5VNkjKaeRNNPZY59C2CscN1\nHiGwt++EjrgOcG05nLdjlgghb8VIk13kNNiqbtz9z1FWaGG/OVZZFzwmV7pPDGiyCrKBpzMDxaDmiDzFKfmoLelcSvgvdDAl\n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\n"""
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
