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
_EMBEDDED_FRONTEND_GZIP = b"""\n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\nZ582s7erdTPjasUblS60CoXc75z9fuTeIwHSoroLJHRDK60kmlkYr9kgKqzmCKDDK4aWZUnu6qzM2dkKPdg98rs0Aj99WKB9\nR4wdvjkJnqyXY3c8Hd+izzDZZFrJt+PVZFolT5rpE3cyn4TkaK2RXkNaGK7Ha3LCJo1STSaA9fJ29fbGnyGGYg9h2jSVrJdT\nd/pmRhisX6o4DIE63K4mflDjNKpSAUEtuJksJ2/IQZrUZu/l2r3x30w6PIK5Fejlyw7A9jGyjIZLsOr+Wzv0mnJmgN6/DtPM\nbg+HyfcKiq4BWU/X8/XlAVmv15dHI3wbButJ92jQSK36RuPm7ZvZ7aRvNIK3QRje9I+Ge/smvAn10cBal+y6vUyCs4UXrxoH\nh68qZ+8SURTDYW2arNxqeWe9S/jSmVe9SDkMmn2fhsFxFQb2LhGn9fjYvipVZKs0iWN7GW79xwj2NGh7V3TpTo9+d7zLFBxf\nvgd0CEqUh5WcPEJHjlVvjC4vx07nRoHZPGJkqEdeorZwvRBuLjpM+OHpQFmSDq68SnErQt75hrbvq8QJa4lsVReRSITwQJiW\nlczTi86RbmxcG56IzoxuXEi7qzPFR8PhE49lvu8vsEW7rgAo38OKukGgapW4cV0q2tuiytBv+K9D2vTgtftFJrSReW9f3Sqt\nDNJ8CMQdo7gy84UVclno2Ph394fX7hitX2atDpm7VILD0WuKq5sjotd31bBhtnHcWyicFNBKNfSO6A+4Ap6gNvzmtllNvPXT\n8A1pxZ5/Zv52zZfWKV2L6I8d5SjONEynI9+ddNhudag4VFkojgKVMxL6pgZhDznPg8+HyrVUn7LEO4Q3HArOXgMQMxoZkkny\nGPkseRVohWg9S57wBE1ETmI/comBL2Nf5MlxtbU5OgWrrW3PcPqtj+HPciNjdJ3gFtl5n+PLxX54sRCcNPMohupCXANkF1bU\nYXVCqr47mApNmSYlYae9TKSZQZm0wzNJU+bwHMJMN7iH5F3Tbjo8Qa4tnW9rVrne4UKGqeVvwrtnqkM+Qg2aqrnj69vVvm08\nvHXtm8jfqo21/Wd4bRSzzbfqli+pRsvs9PWVqS1qV/aGal6rh4bDW9xdyytat7YHN4zU5R5q36qfyDI3uIdqY9bwPNLUNTxH\nZQgbnqU2k10z69CINhxfWKGu4GTCJndFDrTYDUdX7HlflOnaddq6Tf+teFfzKn5Pld69JsmLgTrevcbAPfQL94sidkcQPRpR\n8NMLDOxx9+41PDIuo0Ae2ILeffdfFAFLBBIICQA=\n"""
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
