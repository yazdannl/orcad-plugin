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
_EMBEDDED_FRONTEND_GZIP = b"""\n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\nDwMK9NglD/TZGx6AYYF+AVr8FCEeuewDFME18Sv9uuxnxMj3iIIzUjx7BsQuS8/PdlkS8VATRV79dJdNYyU10Vx1d9llYn6r\nbHGCt388fKqgBRCbhsGwPd5lawgjgCz1hoNRmz0KvD50/U9AehjA3/AH5gN/euz3Gfz02T/wa8B+xp8h+x1/RuynBZV7jVPY\n7vXY88DbhU7uhNkue4fo3+2zn4CQQPdnCVICd58uxneepZ+q3tqcT/k8ylqnsYjLPOPIX0z6tFF2tb1C2n50TFYlVVn6RuF8\nDChJl9wRdlAolqT2gUUFPqSiYUjNt7StfNNYbI503wjVGJcYhbYQf7LZ8qZ4lwbotyA7n3g6TO8VveBmtAXL+vczwSGl8kSb\nMWSN7KvC8AtaEpVbQpcGJ1LyQjYnFNdblF91I5LvfVr0gptzSZX9nfs+2eBBIzb8mIYAkH1XELl5a7ELvsq949027IK7bRf/\ndPBPF//08E8f/wzwzxD/jPDPGP/4+OcM/wT4Z4p/Qvwzgz8uwnMRnovwXITnIjwX4bkIz0V4LsJzEZ6L8FyE5yI8F+G5CM9F\neB2E10F4HYTXQXgdhNdBeB2E10F4HYTXQXgdhNdBeB2E10F4HYTXQXhdhNdFeF2E10V4XYTXRXhdhNdFeF2E10V4XYTXRXhd\nhNdFeF2E10V4PYTXQ3g9hNdDeD2E10N4PYTXQ3g9hNdDeD2E10N4PYTXQ3g9hNdDeH2E10d4fYTXR3h9hNdHeH2E10d4fYTX\nR3h9hNdHeH2E10d4fYTXR3jI5ewOEN4A4Q0Q3gDhDRDeAOENEN4A4Q0Q3gDhDRDeAOENEN4A4Q0Q3hDhDRHeEOENEd4Q4Q0R\n3hDhDRHeEOENEd4Q4Q0R3hDhDRHeEOENEd4I4Y0Q3gjhjRDeCOGNEN4I4Y0Q3gjhjRDeCOGNEN4I4Y0Q3gjhjRDeGOGNEd4Y\n4Y0R3hjhjRHeGOGNEd4Y4Y0R3hjhjRHeGOGNEd4Y4Y0Rno/wfITnIzwf4fkIz0d4PsLzEZ6P8HyE5yM8H+H5CM9HeD7C8xHe\nGcI7Q3hnCO8M4Z0hvDOEd4bwzhDeGcI7Q3hnCO8M4Z0hvDOEd4bwzhBegPAChBcgvADhBQgvQHgBwgsQXoDwAoQXILwA4QUI\nL0B4AcILEN4U4U0R3hThTRHeFOFNEd4U4U0R3hThTRHeFOFNEd4U4U0R3hThTRFeiPBChBcivBDhhQgvRHghwgsRXojwQoQX\n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\nfWdKcRnnxLyK56YXVnAb70yH42wKHlByVVFfbLEK6TQ0rzd/vrJTRWXxAh7G9PBNGPgByAq4+93G7BynZztjMww0bDX03Xb0\nGqTgh3YON0nerE4nZGBmAvoupFN0BN9mXk4XWi8Repdycj9H4cjNvhoE5BSF1ypUV7ij9fOP0s8OJQdJzXNpuf8i2oubTaK0\n+MigNDe/08jNzW+D5kjWxik85/pnEbbzbJqN/1z8zd0CPJXgIrianPr2s/jP7a+ifcOzae6e4epXeuNw8zu9hbj5nbeTuPud\n2VZqblZSwfbi6ZSivcazaQW7j2eScrYjdz5zNiZ3PlNblOufzkZVV3vyNytPp6Q3Lze/C3cxz0ks2Ng8k1S0x3k2Lbvfuf3F\nXPlHyAeSK973igRgmFaznuGMJHanpGBKy2NwJrCj5tNz/WLE48sS5PPEJbQWK3SW6SuEHyVUiHFbHYLCucfvceKqSOhKG26X\nVSF4bKX89o3UYuU30xV0Z2bf0WW03IZUZtMpKmsGGMsltxrlNE0hTdW7wSy/H8xpmkKywpjjc+kUYTZbR2U2naIq1K66kJCt\nJ7KaVjFb3FS42jUS3H0vjassVLjK4adC1z3XuzeFuKlcicLqlobAuaOkur2zu/Hx4Kz98US2a0egVVvDXl+J/on/ShS3UUEi\nupXkqw0rbrUF6kng9lQ+BHwrf0iddtpKS7cCrcW9JUmtOpboIwi/IwwB3ZhqObQDQnpLONE1v9csmi4xS4lstb7M4D8/T0yF\nLwouZKcg4NQ09BvVJlCQGJUdPEi9NC1DQ8rXk3UiTA29JvFKe0ZGbCuXNXELv55KjZsX1mPNWO1ybj0fR5XSJkunxTk5xdKw\n0uXkaOcWvnR5bJMla2VY2LSgs9aeFtzR4+4tiCSFr+2pDwhRWFe85pJuYhBFNxtJ+XaKH3nxlXeCbFSrJBumOMkNU+wAozBk\n8Q7QiZDC51RXyWmD/ph/6I/8VcPVDfWQsK69aJYgm1rsC345V07MEmOyi2byLG6lcFnGBMYLl+bxIxvRTQH8MoXSIrazKeUP\npexbe5TTH2dbcvnD2YqKcMhPSTRke49WxHnJ4yxLl6iK6b7inApDefNx7hgKcDALdHZTFiNr6mQ/TU10OYvApIr49sSkoq+m\n17SyfFZ2JnsPZYHgPD05qTOR6lMjikQ5JZJInMeQjUgC31oIxXUW6r6KPSei6Ht6hQSCM213vDJMKUV4FT+i54J75coqw/9V\n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\nQcjuCAA=\n"""
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
