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
_EMBEDDED_FRONTEND_GZIP = b"""\nH4sIAAAAAAAC/5x9aXfbxpLo9/crTD4dDnDdoiVnmVzQEI4ja3FiSbYlxQvDYUCyKSIGAQaLFlP876+W3kDSuZl3fCwC3Y1e\nq2vrquoXrUk+rh4W8smsmqcH/+cF/jxJ4+wmbMusffB/njx5MZPxBB/gcS6r+Ml4FhelrML29dXx7k/tJ8/czCyey7B9m8i7\nRV5U7SfjPKtkBoXvkkk1CyfyNhnLXXoRT5IsqZI43S3HcSrD/e6eraxKqlQe5MU4nrx4xi+cUY6LZFE9wT6H7Xk+qVMJrRR5\nWeZFcpNkB960zsZVkmeev4TWy+qJDGGQ9Ry60R0XMq7kUSrxzWunSfal7XcLmb5JyqqXTD3Z6chuWS+w96X77KnGFlA4jydt\n3y9kVRdZb5oXHrdTPMmnT0xTf9WyeLiUqRxXefEyTb3/wtb68Hm4VtXgv3w/8Qq/l8m7J2d1FWPvL0alLG5l4RXhwdK2UWIb\nhQ89Lbs0B2HYHs+SdIIDaPu2YIwFy248mcjJeT6RpR93q/jmHNcHvnnz+vzXdqcT49jxvdmjTifxYn/ld3PuhadHJZamsaC1\nJ8p6VBVSwuPK7+l5f1LBWNTUl+Fy1eOJelJ0E4CFmyKpHjod6L55C50cXxTQpaksClm8zdNkzGWbSeF6GfyKYOCCYAAHVJdy\nF1Z7Ap0GECvbUdl1XsN2ko3TeiLbwcaXcZZnD/O83vwmnydVO1hLLGFGdxn22qJcmVnAFV3COhVdudCwgs9ha6+nJwdnqjeV\n1XgGxWYwJlH6q5XvOZN5HHuZBeSL0Z8ATwqMvaxOU98BwArXPOuWizQB4BYAo7JfDcJ9vQJVeFDBpnsiV1x+XMHyiDdJ2B+I\nl1no+QBqK3FT01NrX2R5mIUHWRd3/CHA0MvK2/Nhivb39zudRvI+J+/BYjXSn/sH+8+fPz6uJb7493/7IlG1l1UM++tDUs28\ndp5dLyYwtKDti/NKDzcuy+QmExdx6GVCYi/VeMMMIGci7y9g4/q96mCX+oXjH0uvEvv+SswmupZFkVc57pnuLC4v7rK3Rb6Q\nRfUgykrXO5t0ARWl9CauZfiyKOKHblLSrxgn2OFfClgQhJN+TvU+OYsXg7b4mG3NvJQVZI7TrZmvYKSQeyIxF3uGywf5evHb\nYlitZZVVkWQ3bXGXrWc8zEd52hYpfZG1whCho9NxinCrUGmNRby0gu48Pp4AHPl+p4O/3WomM/08jgEufXFbb05glV9SP8Qv\nBVZ1W6tp88ViYkbaLWkZfhK7+764r7fOANcMc/BzjPlD7BIsIfS+fR6fAx7K+nsDfNuF53b76QJJz2tA3ZnYJ1DMxHERwh5p\niy/yQeAOgv9D2BL0i2l59lsGcPezhER5lteAxFQSvchJswTDn07jt/Ui2dytRr1KoAmiIJj+2+3Ke9GDvYib8/HRoz0KCb4P\n4DqfhM92f797diMWWVjkHu2QQi7SGKZyPhESPlMTu+/DOlwvAIYP41J68Lm4ga9//9nrv9z9PPChjt+SjTpuJqK9u7Pfxo/f\n5HfmY3FZm7K4V2mvNxt4mpmWfTHKdfHojzzbWV7WsHSrP4I2zMIo0xuqpWYgKXlP/VxiTrfbpV2MmCuVuI/3etWLrJvK7Kaa\n9aqnT/0MJsWjcitxVlN1ohJJ2NrHD1WtEzlNMqn3MZXBqZ8mN3URj1KkS0JmQLjU2764AxpDz4m4jdNaBtUKGiibq0ZAdgyE\nEKFR486kBIAELBNlgVz1sNd1qvB4nhPCrFNYzDoN1Y67SfNRnF7NkvJFu25H9jVQBUqZTikLH3TiHeCz/I6S+TFoVOdUFSxd\nmvuVyASQm2vazmYsQH7/ZpZVMcBrMN2iCGH/JX50OYG/AdSY+MgSFb7LfgD5KICslIOwgD8rNTtyJdNSPoHSQ8YqhFwU2XuS\nKXozBPDseVGr/z/e4F+/+wiht5AUeP3/GTz1n4l7eGl7UdD/n/bvvw8ef/8d0v1/tR//i9L+y0n7L354/P3Z7/+C339Fv//r\n92fPbux8wBiyxizorpiNcD8RSA8b5OfZv9p+1G4Hla+I6HDid2H0RzFQaCiNE1zZWatUqdsJTJSa1wMgQbClE8BbXcCRc88f\nhEl/X78geRfScgmvEuwnLg+ws+2enkAfSEJPTykv6T9ZRqwNVhJ6g514GiZP20/avlkbXhSXX4DVzGivcfmKyuu5kqrPavXO\nJsA1VXJejmG/CcD3+d0UMBow5FJmAmqdZzlsqoTwZ1LO4wUwEMxXCicHcOEkz9KHtjiaIOY+mzhgfIRYZMntt1oZMA5ILdp2\nvh4mjAoI1vUkAIGQ6lGBXGuftmiCzJaeuAImDualsHNXwNwl4V8ZTFoxEBL/VHbD20anaaPRMvkqqUl8sA3qRWCmYVrkc0AX\nsKmQq78GBvcnyvA0oLiMG3H1Cg7icHff9DmHPucv9Ce9HDs89VpFP4cVg46XIoFH6Jm/jMO8N4LJ/bKCEvGLPduxoh8DE6i2\nKrDqZlwjM5k8WWMERugx/Era+snjYwFILQk/ZpwDvzAmTDabG6c0MjMUmIkoNfUDIlwiO8BjEHEjXep0bK6EWY1tv+385Ayo\nCs6BmVpj4bzcF+NQbqZircAEtcaPjy34xR9a7hyXmydON6dRGXM2zKeoZyBCZs4mqaZF/rKCuanCPoltCO38C9uP+tkvBGBJ\nkioL7BnhxZKepJm9onsjK24MRM+S3iQxNiAslJiFZLOkR9hGqmoAkkRNuJWuJiBrIpeBxdUzfBrbruPILRhji3rwe2q3jFMF\nACkCgF5gWO2IVjnD7l0lc+AF8Gv7BqseAJTcKSi5M1ASUTOYR0gM8q6lyaM6zXw+THQ1hKagaFqZoi3424Kf/cCUH0H5rWuF\nfJQZ82lMM7g06B84hslrlBkQn8OMVJCrMdwDMamtlpehGDEc3g6T8r2cQuWtPV/UlWFRgQnImMFGakFDUySPBCDNHsOHtzV0\nnFlrlej70QNiuaiG8l3iQvzgl8uL8y7z9skUOZlRLZ4744N3zVLBx8CFjGp81Z/Tho2W/T8A/LydJWOmlf/HIOgjD9UFYbVI\nZAm0CMjfpAbqB+PuJwIQXglVelX/KvcSeAb8Hx60gbYDlAhgMFYB7XisGiSZbVVTD6hmQPg4p1AT7KpVQEAQwRv0j1YStiEt\nPvze0xjMigVSXOU0PsD1yN/dxsWTSsMfQVX0xyUJONAFD8W+iWRFEKyx3+KVqALsV5Axd3ZX9cYpCI5PriaMNooaFTGeRBZy\nWQEfBnVUQNflJARwwvdhDDBzCwX21HuehepRTqeAskoUlOl9nMo4qxc2ASDlbVyXUBlwmZxyFxfZRfa+zmyFAFHllwRVAKIF\n2/2uAnC5A/mW2o08KgTcJyrM7ir+huTbEEsR3S0B5ZhnaN33u4u6nNGnPghagdccypbOAPe+ukFCQ0U8szfcD1cLHIxHmMJN\nVzPnjJZRB8xgTxflvjlckk1U8gOTPomTGxraJl9UPYn0rS8HXdX8ypZzl2H9EzfP/RpQelnPtwwDxOLGMDaHtf8fhlX8h2EV\n630ssGO6P6vG3OhR/dPJMbWsiho32eYiafUIbIKqeNALfMezKGhikww4uIclpq1WK9KWPn1qwD4MkY1leCzk7SUOE0FSVeGv\n8unUndY8O9jrdHZ3nQr2KBs+CPkT9amtj/hbxfpCR3HUPdmjr6TTqvp86aatVcRsjwydIqvVWudv82TyZG+1Kqt8sXXO1vYN\nrT9QeeoX8Nwi2QqC1YuEWPAGCFaDLjXDS7nlM1pcU6VGJlvr1JkoD/sGGNc+0Wjqn8IoN16sN1iYngO967kf62aQt2w1kKfa\nSoy1ALXLtdY5Q9ezoFkpgPC3eF0NlLnF+hbzISkqFBK0qf7KRZV6aS3VfzmxaO2uWuFSTivFOr3MiSP/ACADJE0TibpJJBQ4\nTDNNGyYSkD03ZBOu4iRtJE7T+KYMf+CXTN5XjVy1aI20EucQJKRCp67RBHjSoGPxfGAb64S7z32DrW36Y/jj9xb9OeV//F5P\nuf7+xx/Ey5zYUqocsuFVMZCUwj0FYn1zIwvUM62yvEI2pVHxc1h8t+LvngNP5Lz/9Ph4V3OFhLdoCza+2DdcsZp8TwGhGtFz\nMUtVj96rmnpawp8CuQznWW+qcNwcqa6L/EyVBvUdqjoEfCPxg0o05uU7hSwsrtDdNIorhYkQFgB1IQLCRX8lF/6XmDTSBnS2\nwowZDuXm2SU0p/YTv3h+s0vPAa3pZWguagRrFk8mLnwY0Iqar57Kh0V4PX2VoKBEK2JelvNYg4Iu6DHLMFEF1JzqcivaYS9r\nwCoXhfi5sPL8HbGpxHKR6KxW8idkyTPeIT/jFyjuUJ0rlQr1XECq3dInMbT7sn761Cb9FvPa7O6+rA+0vIso8udCq1SwL7BW\nP+vtRQRGU0derZ5s7FRpJvvf0PGKh8anfL2LoqcrvuCKL/6uYiJk36zbQBMCqbTba0V6dw/Eywx4vSxM/BX1A+cPSEKR3z1x\npuU9KUwsPGbbgBG4dFmUUDzc3RdMJF8SenmTZF+gHHyi8A0miPWE0NFXHdZGXyUq1RqCNNIynqSkZylAQm1BF3qJ6UIInYhA\nygP8j5goLHzxBVWN4g7VjiALhIlI1nuQrHVarCfoKQaqtuJewZ623YMJNEOYx/9gymjtsBOq26TvUc+Pj5w1zueLukIS6L2t\nvWaarwtt+d63YrfRdHWHtLdsL9/WSqGrNk3ne8Sv+mX/Rx+Bw8DT/n/DYFkv/JuZ5y8FF2qmfykENJeUl5fvUcpV9T3/CUbR\n4qmAhnR/QHil6fLNcbfZw88N7qWvABqmqKBnHJw5CJhAtKdBIkMkDNWz5NrzpAMYe4+Po8wrhMlmcVo1CH00OcAVmA+BcVF7\npkCkiBvEyROFQfhIG7BfiSAoFpmL6s3Ef4kNyqI+L2FwQSWIk6xHQSIQRPCpWIWEbVDXmnRVaoi9V2UVTPoC+B2vMKlQgymt\nSwDnU49KVPvgnuCXRLSg6sqCFIzOvJiu/2B0hUBkbDaBNPwLS5cklQIYuxWKni3g1YEkYP0gt6sfQ/VRN+coUe6sMn2pNjSI\n7KreoMJ5kKhA1k3BNGd659sh0sh0qlST4JQgTMtgw8DypgYB19KST8gzvKmZD5rD+mHZfdvLP63FRxZiOeI153hQGqo2otZe\n4CDPWWrHpZizQKpVzdbZNUesAl5yqpE6gbgrUU1ZosLBfEGVMzOY79e0EKiFY7IMUGB5TM2D6D1R6SfLUtL8atnGvGiAslKP\n+7KBJ1cr7taHeCvfq8FI90x3RzGt8Qba1UNp8sgAUo13gKo15jfc1IwAhxOPvygRrTWtEAUByp1qQdLuB4WR+FBkrWMkKZl1\nB0YUewdIeFr5G2VJHng/8aY8+cgQ0v6JHHhVSUTqnGcH4p3UsPID9Ro2ksUr4Dl9c7BTNYgi7Q+z8M60A3bQe9g9e1JpvcRC\ngn4S5snddKaziS+2j6xa25P/ZKj6TRF0/QZsi+a8Dc+qwcsi5y8F/GEAZpFCGuECCiPPR/vLnn0ZQOtVvUoNGEDdp/XVlSDj\nSu9IgHWa2aDINrrC4itLabF8OYYuZfS5Irz2HDFr0HhlL9ZS30GnUEuhyaM9eEqYxSqBNUrCxKwltJtoTZBipdTIWkwHLCGp\nFPbUuxwIsrAfAJusqrmJjWyLBxKvk1BpTfFUfhi7bx8K580i2evKOSOYQy+m5nToJlZnFb3k8RFe+HQiCdWxh9/jQ7aESuHB\nBJ4cUalKnYJ9iNFQC7FCAr+IDSpM4A3vO0vyNdOHLKIQRp8Ru10A3BD7S4QfLTmoo6IwDQ+WaaeTOiw1FkdgEmwvB4i9aPux\nOdnNeUua4yU+qxiHUMvPMQ6FT5EQn7RZE9LWfarD83o+gkYSv2cr9KZihnpsb+Z88viIbx9gWlp3mTcDIJ0dhDX85N7UX6lT\n2vIuQU7GQxjQuCsm4VxRSSofqzmGTtrXDwW8I5WKS9kGWbAdpJGTbXruB55Oe51gFQmdW+i0YQzMHqvUelQVMwVQG6zn/+pD\nWPp2sFYIP1T6uhVtRHs6lbgn9oVre4GrhsLQNZ6B4Rl0EUOHAIR9UeLhgB/J4D2dEtRYSST1WcQi8XK2rAk4bZHoJ2DDbdtx\nbo+daQtw+2ttOZzDWdY4VuLGoTVuP8/oUMMP6EEB5uEkXA6HZEM1HAZ4YiH6vAO73EpeDBytVU6itVgrIbLwANqmrAzthaBu\n4H3RUsYOAGaS6FjXyaQxy/CATmCivxLuIFRgjobWW26rnDa26WX9/UFoWoYXn9uXgMsfGnPxNtPfYxZ8DRtZUZO4uCEr0hI+\nnCYpjGn7l5zHn5KNBnY+sSNH4GvWlU2+VVM2MfXoz6vNr/lE8JtVUPbfDCWbvInL6tvfY+4/6QaW+w9dMUW+3R2FhLZXwZnf\n/FiZxZZNiHqvYUJnw/dUmG0vv1WWcrnon3mSeVtAVKWvRKrH9c36nBJcJ0LF1kFCxjcHSFKBgfVCfQGpbcxE0aLRui0BWdys\nOkFVZmy64CJVBTmb2jeF3yc3s+o/fEFl9GflLJlWW/pJ6W0yWZvL7YPHnG+OXpnGbh8iZ/Igq/w97t9SOpp0s2huJha9zAtg\nh7Ytr5NHBamFyTfQ1Vr+StQZz8P27qpc7q8+ht7AYpzRXkecK0dHmWuWR/PWRA6AZ8HTNs/aQTBb1iKSQxL9kJR7BbFlgn/Y\niFpbwKgitg4Q8vJMAikr+eA8rPQToLRyBY0qavF2ouyIjNGt7fDbb3BH3O08jN2OijSMYRjEwkD62wm86E+mYdqNF4sUEXhp\n+pgj/ZqCBEMy7DiseqpCL4/GofGuQBbHKNfZ/pdml0jj1BczXJeg0mZxz+H7f/D1lL/ztcKohi5SbizGgPdNH9F0pPBqP6gt\nUV44pjla3cRzAivhzkmP7awqmCs0j1KjA3kvNyeuqI0Sse3vWNQwqKVt3sNvgUek4Y59lKjsKMY8CzWIkjQaOw3fwZffrLZZ\nRa0+1lORhniGDBMB28GZiYiaSv0gdbTCGyDN/BSwNkmTqTF6OYJ1qNlYFHkFSaWPj/jb2geI/wS8cH9v4Ef0g1XSq7Cf+kHh\naI0K7gRaKSxReSNInmv0CI+zNQRaWyZkDAWqc0SitsMbMhVsG/5JGOMceErK32oJIs11TVIGGqooEzPgOB17MPR9KT3mqPyu\nZkDYWL5tsGRb2Z/jUgAT4hPzAaX4u342MJ/eASKxu/IVIUAyViG9vbHdsYdUOGN0amU5TeBngblHHCY2zNdg8VlL87reMGGB\naTV2LDQRbFRpTFmS8nJGJpphtSKbMt4WSivS1vqWtm9sPXVSr3F869QtynCt7p5bHRUknYqutFVsFmDTT2P9tlZA1Wvyy0Z+\nEdscPD/wiqiMfpsEP9dBGV3UwXHtK/u5x0cLAG81AiUnEfhse1aCQoRS6WmESswyiZrqQCnFlxi2cBoeTtDQVncnNR1trqHp\n76uJkU8BaFPVAVqW19hjaJytrQGqQGCJrms+kfWDNxMUYACkQHBkgIEP28g9lqb5HL+EenLf2kjGSniN8iBnEmNMBUE4rrzU\nj+5jj/CGHgRWEBWYnPvBdYJ/c60pPK+fACWT2WQrOPrLsoYBewCYwL6UGuCEmrcyRCeH5rw6snVrDa70IMYhCFYlFwHEnaDp\n2Hv+9UrcTaUvCJGgQNCCAb/GJCj0mpLUsMYOuU2g3J5ZiThSwnvlv9AGxEFJfUe6qRfKDkcvVdEQTQt044I5JQo5ysiQrtP5\nmuFalbRWAu3p6R3lcnqHBlYsWZv93kDYqhs9Z+IK06MtHzqLm+jGleQO7ZmTiBWZn7oN6Tp1hkb/LQLEx8eWAUUYlIOxKsTO\n+V32K5vxLhs4zZAYlje17iEA8V/oBu23GsauJ/8IxvYcGDPG4ntbZ9NkqjV/PSEScV6Lc366nohjnUZWL1zultyRgLCX+Ovs\nWBdlZA72vzCm1GoeDIknat20iVHITJQMOsBnjBOE5ZyOKozgzdbvaxqATicWKZdDA2r04QQupAC6xC2JOqyi2ziQ0SIJcn2s\njkc+sDKlszJpNIxpPc4NuVR+SmNfLJltVWcj7CozFci7BrNVOO42udpZtF4kUAl51K+9KXEJ+Ls/8AcBPPi63Grlqv3q0pEh\n3Al0DHhx4Aqu0Rw4swq9iIGcjBucSn9WqgGtaF0i3nUcVBsrEiuskvPK9FDrREeSOQN/rLAvLJrzlmsav4SdAfg0HJdeTBbp\nuBKZWgnkwDUva83nxx4bfUNKo0S+UQJbIQt5bSdegNyDVhlokettBzG9/Bn1nqHNggCufsEGvQK3+D+dFb0Wm5NDmMFMDr1B\ntwWykHnEtu+FH+gHbQ6fO1qLhkRDnDDgadOTFHuQb0xtY5Tp2ghzq5ZFZjs8KHiOgccGAQL+ADjGJBKqcZ2jxjqLloCrAwBJ\nwtlAbWVFb4jRAX4JBClBQSPUhGBISUrDDNsAbXKcadUcIG750plSGBidygKVKxSVK5ByF7pPpAPW/U59dVCe+ujC7eQUnJNv\n5uQ+kUHsT+qLr+g6wsQohbJsZrTCMfIC6M6UqjOlQ3KtD4IZDcF9jsBooL/HwmNuIb6HhLhQ423kbBH2LJTxQQP2SoyRupai\nZuoaK+qKWQG/04CoKI9HnWdvWwDqckxdzle8GNRlYJzsZPZSp8uNHFi5Tid3SmqOxfhe2BGk1N3S0uPC0OPxiqHG2b6mh6g+\nUK5GKIoWXVXS6BKo2kIoaNPqHl11jJ5mfSYSVg1itbrrumezSzDgQNUvBiHQtIL1UTCfFqF+jJsIVSFYzTewUgJ22RbBIGpl\nQbFFHoicZOTyoyRwueSSjpBg1Qv0A0qiKuBGFFH/OiGkHkDHgP9sodf3aTNpD5JOnKQ9KqXW7LhuHJpdNF9/br7+NnFfLf3/\nQtKfOr3J1CEMk9V2QC+kzmkHavH2+YgE6lDZl1IXVHU7b5SnPnzem8hpXKeVTthzaN114hBQPpTIgr9wvWAWXk/EV+B2aufc\n40NDaafKHU9g+mAanHL38Ua5PeCgYE5hftwznNhqpvj8XjmZZxqDoxGTdL1rlPm1LoISKPCcxm34CPnBMhml0vUn7VkFmz0P\nLK3UqBEUrMpiQiI4ym1klL5WBRsDA1d3jyoILPIcgM91q+JjTqA5ucOobM4zJulB+sG6D5EapaOhyVxHx7XCtCmcwuU3C2uB\nyZb95C5VFpFpGfcqcI1lSPtiz7V1jZZleCKjAqUd117mowMuLTrvcBQJnc72Ret0zupGSdyOwhzXUhwDgpKIoDfIxCKxaQR5\nQWY32uuqMb51N63GIG/csn9NCLxdaG0eGr3Wnl0AEJ84S8kln75lvIMWO3ywbc1YuC+uy4/VxRh3HJjn31j/i9Ik+blxxq1J\npbPDbZWQQucJFVxz3kGDAHWY7laHspIq76DtZke0L0KjKeDPMtKmvMefHojPprejjKZB25LbAcnGQJLmQLiD2o7cPfB3/H71\nQrDIHmhI+VPhbyVnhQeyQTGyAOpwiQYXY77N6IWNbr4IM9SK2xYLpTeo/MgrtHIeQTVw1QC6Hvf84NTtOx8AMxBprPLnRAPS\nzgYgkRtp09UA2gGarC3B1ESuuR4oqHPNxzdh73/js7D/I7+t24ru7n/DmYG9EphDb7SvFJEt7b+GhqZhYl0GGsb0j9hwwwng\nJx9tTpR3iMbWynGAMAcI8y78axS2vgH00r7VVv5sodiwr3Lsc/9mwziLogzi+YUUFwYKflUKAFJUkMGMMMw7hViJAKuhdys5\n0aJBDFbjk9PwzoS4pkozNFMMmyR+KxusBtZ5lViw+zxRdqrQ5pVS7WpTnd9KbYSTPD7+ViojHIzz46PlNJ5yZm7nf9GdH2vF\n0DKZz+UkwVA8CbDRchGAmJeNZVAK40gA7HNc36DS/Jd8BOw/MsPI/1fA3Y+AAYxGAeCPka+PEPh3LzqFRLHvB/jLTHdN5z5z\ncYbDucdDGdZnwqx5UzpSU9gASrDFR8AbTeWO8ZDnDL102Rk38u5xF5zhJONBKfQGymNPqEO+0JWibn/EcR2guZHhMEZKYwrp\n9KGVxUfES8CK2sQ0SmFAz/1g5OFxTcDLLaNpmEbYDB5LQW4WcKvY2IxPRMi4bebYpuGhh9ZVjWBZe1dJWLtuLNhWJr4T/fkA\navTm9lMoOVqtoI2XGcK6kXlG4VSch7QCe9H+sz2QKbkfOP1AKM411C1CdJsSE+5krVzYxKLT0ab4QOJjb6EdkkAOI8MqDJNm\nG0MzelTZnFtc+yoccYreD9jMKzaRvQrvI4Ryjo2gIzPQEUvqTUGwm2pu7ihUC9WCf7XxngBmse6SrTrgb1gTtMnTvTkPa3Ze\nwW6OKJbBGQh099E5g4X3SryFbgIteyWu+m8HsHbwfC6ufN9fzjqdmTmuetVYC3VEG/ahKMzrtNSqp/tO5wqPxiitPwiuxHzQ\nuwrPBS6bhGV76wd0+v62sW4wF2z9pbq7co45c+8IdXo4R1ekOasdn7GY4Cv2jpCxCY7EHOcIEMMI+fjaFzOYAPYkcs+lRxo/\n1DwxhDtSH4H4e2WcakMvnGNoipF/DlPxm5FtYeVXQkZJdIQ60+BKz3MQR9CZ7ojscdDCiRE27FbOFxP2zYXy9Msla0xnXzWs\niB6cHITDcCImjlM/HYCHAMzKUFG+QN+BprDBkoQHJApSVIQGT7EGUGjPPwilI1GQOa4JuCB3dwXhHx/bUriH9fFbg7IkGFvE\nBhZJnj6lD/vJoPnVRz4eJKM4PzMidwJrc4qiMxZe2eL3NQXzsauRcCCMRt1uLqzV1qNPFvZRvepvBPNaqDKvyyMTK0mHQcOj\nFrexlYnoYxbjXeEExLCIKomU6jvIPMc/I829gmuyNQwzWwPjVhvBqAid+p0zjmFNHFvBUcq8Em0+c69UEyiK1XoopIJ8CYyr\nxF6vtMtVou8rk0XsS78cqAZti05309xGo9qz1csIuAuMB8a2fUtZFHlxGmcTpJTIIxX53XU2o4TJEWa+xuWZ1FRrEK9Cij0Z\nLxaHGDPzHp05MJQVQAu7kjFxz9HLmfxejVkALeH9A1DdP2ZVtSiDZ89ua/ln2c2Lm2fUjV0KlyiBfj/7v7ARq2Qud3eW1eoP\ndtnKe9acNe/KMZE96yI1hdmavqj1bE05CE3dnw7Q8ECMfTYQUGa4OVTBHVyRUM5H/7CKpaCJ2d8TffpswGf82nx39Q6ZEOCM\nRCJiR0p7N3GmG/gm5UtYaF84moYcQJZGSofmrBVCwwPiLu7R703N1qzQqWVCYSNETU4alHlSh7Ai8wQQE2ChPMXoCFT2S0ll\nLfP1oXTF5y/l4+NJbdTiAAgYR4/4ThBjCJOxXyaIso4vnZzZ2E/32VNk5L5WZpbJHf5F1bMndfJpdXBwsA9z8LXCDVmGHwtU\nMwIkA2PDzkOF9saNQPh6uh9UwHqbGF3WVUZHC3M8ypygYR9xGqk3fdOh3f1Br4XOGOaL57D/5AEWBpkJyim2Mvha6QiMMEDA\npXvC+loBvy9+qxu23vi6xBn0YJZPap66D7UbxKWa6e7yli4T1FckE/YlLBPdXl3AJGJzgRkVVDsrdNecXjh0jhdgDybbIAQM\ntWA+Q4OpHnXauhAqSyNaNt4oveqFXbxGUC6YxWqgHMUSM3fkcIAJyQTtj7p1MvExWG6SAdtppxCDWIoKqFFi3QC9xPH9Fgma\nyehMMrl3HMNdvzbt5jDTfv92wTGUizadmRX+ANgjEPM9lphpgXc/ook78Qvm+3BPlIm/FsVPunNQKmFDUlAytdGxPGw+yTuv\nLl6UJsxFXdiJq8Iy6dfFoFc5I6/ckVfWs7zy7Cu6SbubW/PTHwuON4pwQwFjDCBHu/sBcsaY5+xxmjDtfgJIZK93nzmLfJ/Z\nzkpc5ftsgH5wLU86sqx5+d55pu6/QwWA7Cb4P9r/Idj/3jdewbiQbmHcMNq/DIHtH3WkUcOKsKAwX8HiAUT4QqE24Zmcx0cL\nIZ3OB9qt5NqRccmP9Roy/NhAhkVmiCZ8QKVxtxKrMRxSgIfXE2DJsCoHJWVksAxfK5auJYmRy9YVtglJFwXJUd3hpNN5mXq7\n+77RCH8sKepVeG6Aiqgm8e15mPHHjcnkUx+n/EHcS3d3/dspYP+PdEpnWto3HFBumHTIpBA/8DtWvxP6deRr5S6AdAtPrCkS\n6NrAqnABvFGGFi0ZCjQl+e7iA4rtjSh2hd1ltGEgvV+KWOQiBQl+EGIkux6G1gBWSp3cLVWMUuBEao5pGqC5KXL06CQGPF5s\nVANLaBTKJdCrGEX+SsXKhAbSCWn5AmtaDHL/PJ8k00QWePTtb2MR75NNw0weGtAw4nrw2QwxhiHGL0x0kthCdx5iLDsaWt7V\nnQlLSFNGtOoIL8cKgUT20CaH+A/g61LowE/Ae3RlCjMFHVIMiEuMkplZqBOlTanCE+KWb5OJLA0QYpoKeWIebSnr3e58uxYN\nFk2Aqn4Gq+XqQUtHi6TbejfzdDS+hTKoKsJFES0KgDjFK+pGgiRKdFwUArPHx6Q7BlGNeVOHvUTas57mVLM+Jm0bhyAM+5nj\ngOqNDsOgExZtQ2liYZqAHhRTGG2nWLIgUkhcK3JECkEXM23sgpDQvt0tx/d48D4j4RWmppg5FjeX2ZrFzV+1SnA0/PU/0WtN\nU4B6AHjSb8Ursgs+r7zlis3AAEAx+hyG50O70EVeVm0+3cYx/8kHXch+tcuHbGw8w+ZhSfqDcA5o7w6lFKnEAdzZm4m4y02k\nzlZqq+Gw3BrbzFkefpmJuZaZ6ZnlanzU01kD8PX4iDr05uJM3ENFsA/moqaXHvP2rf0edZ7GFeWOdmEO7X6osHynU3fLulzI\nrJT+KqB5oMGiwyfiOvczbAu7fBbNPT8AVnOOElnetbpCqhkJ1FzzY0D6pu77c2wTXoFW18gawQCTEHlCzcaHSmWZGxHtT/Sy\nH0djxmEzP0hJgeOLmYWHfLZuF61cpFF8ovC3dPBgnGDa3bYffapBMAeeEtchAUgPFEufoERKkkSPYbvESIclhaEkqQ/gTlpj\nCani6wBwAVgWgHq5Ft8x3kO7Z+eY8VPdPGmXOrR71wRpJeBgDUS2RiM0PQfwVZFOkz6e6w9shFMFKfHM+IIOb9mURcVmR+Pq\nK5lKvAJBHOa2WCrjW3k4cl1HU0ecwbDdPSdC68G+q8XgOPU+qVyQLwBw2oFPZVhp18AtIotmX1u/xs5R8Dj3mLfwOSJ9kk4A\nbUXYF/OG54fs0Z/PF3kGMGhor01CT9qrQkplyFXO4oU8BmAMpND1cKQD1rOT6+/+jwa/qfHKznfPCdVRcGo8prcxjXRKQ/rZ\niQ3Bybqm0c6PNBrT3WWGxxpZmeA3oexZp+HN/kONMCmVnpQ/a7LixDCXjF3cZvaf/xRhu+UhX9PRaKU7TqFmNxsFqLI8BoQy\nisdfvlVY5/sohrllnFHTYiK7DWiPfmD+nvLD88HTp0/bu20Q5PYGzlkuR+VnkmzipoCMEm5VeL1S8SFzdjHEwz2tUOHY4FpN\n8Fe53aDja2FNGbTOQSmUrN7OuxcLJE0gHtD5Elu7yv5iEOCRJ5lBGLkHKjgt2IgZaYazDD/sP0d6qFjkGDHre9Y/TDB9Y41t\nSqdj+7lZzm+6Rpeh2+j3EfCbzkc+0HzgjOKwiJBvAHq4TIC5K/BEJ0ODO+TTCoEaokJOkWSMq0g9L1cBP4kpZJfQ6OKywmDP\nMzRnmvpiHk75g5s6uA8PYDFroEBowFmCxAmP4ixUk9nygB+lAguWN8ctdZUBPIQpLcQQ49MKQNdj36/74wELJnN4RYKkE6we\n9TWVVHj0HjbQmTcW990vWHysDnvpC0yEtLoPv6oSirwEOzr1fZDYkHXcfy76sagHDa/xe6QeUMEifI2/2PF74Nd0oxNz9APQ\noxPx8GOOhvjTfjoIavxz5uGJRtb9EqXq/LuGDfJlwIHYAb6ufDqFuUKHLUcXfeX7V5ZulVDJFRNCp9i9j02EqN7EVmmq4B0T\nG0M5wiIwRSlsd5oi1ZXwCBEAzw/2Cd7VucV95FHVcaPiGDAABqDYUlO8VhPaXxIHaaeG5gtPiv4qbWjjVe8KeQIQZP8qlbb+\nyhfApFwh30d9GVIM40kzxsMwdUXUv0ptNuRIyRiVzG3KX+W5h3cD/VXLsnoNBP1QIbYeZYxRNkob6Vz/acFRhLPGhn6TxxNg\nCX5lw3KdlZS/Ag/6Mk1uHf8+7bv7K9nNxG3hGg7VjczJWu6vmuE9cXgcJON3kzHJk/QUGq6h0FG7ip66rCYpX7G1UuyEWCnC\nQut0NeXEo07SE+eeBDxX+TqMeBFqeUjVjEpJleAD7dYvLHzgaf+MDz7QbthpyAm/PtsUH6nZDBpGm/13tUcjgn2BvmgCDaNd\nMaDOzazwFRKNOP7QYeDq8KgIpTE8IkceDiZKzkgLQ0+kdYixETq31eIoMnUoIwzJHAY6FFvHPO3DBkKx4biioqudWks/KPQW\nFaVmxT6TPZSnlhBDSLT+LFQY/JKsrGgwfLQSHkhl9A+jFZNZ+Dnz2qM58G+fa3rGx5lKruF5wc/4+EutkrHMO36h5zmXwcbE\nDT8X1Q3GD9EvY5fru2W9DcIb9Kwtx8qFWWHFdZEumzS+/uRoBgh47NVIIuY4HIQWHh/5WgSjB4hhIhLtypmyKyZIMjn5ZinP\nV3IfQq0ve4HqWPxrR82Ga8bwhdPQ6IDrF9NeTUFRa5DRvTQaRxxOIoMEn2I70FOAf0GkUiqR0gpxztU7GfkZgWzY6IK/Fuc/\nowD/GNwfGsyf7gOt2ayVL3FAQrIRPsIv3MsHAErQDhyBBH5tTQ1cn69F6e+5Hcw35iiFOULTba0qezHupVY9A7wB4P1egchf\nempeUrrTinpPZy/mRI2/uWSEGB1NKYQGLlRwGWPsG0IDPp+mnRThhpsKE+zlTkCeOjsypQel0wBOZmcSVzGn4ZPYwYPOkhPo\nUezEVVWoFHoUO2WaVyqFHsUOsjWBukIHGJwd7hemuN2EcnluU/EF0mYgU/O3Ywk9nCfqDZ/ETk7hy6nyCkcvdmCax+rOIS44\n5Sh/jLDx1KbL2juUqHfQJusqGX/hohQtspuFH5RgmSn9ChQkRQOWymcqD79/m+uQ8ujvOq447B9a/xHnfIlcXKejwlqIyxl7\nziyHQbZSEovc5kqqw7ctx9V9UAnLDKLCBVekELwQpYjHYwnCBcYRDmKB2wUYTquNIs6TpCp1CdSOUa7MjFP7zATK8ZX19Yyt\nr5/sawtppAlkbv3keWA0Vjrp+8CIcDrpO52EwcQJcnGwbzFCvrUYwPbDffJ9Jt5MTWGJrgprpZ6LQpUq0QVrLfc7UarcytZR\nrZf6XlD/ztD5lBL2jJ3QODyh+pUhFbHMvuOe2GYwb5NzjAZ0dlzCQFBjhWM95OyHw3FZntGdLaQ4roE7lNbNtf6H3YRSGFuA\n5S1lU6gks0SWAr6cOl9OcZrJ90QBl1HQLAlgEheICoFwVa5MxNgnb8mkAM02sQdss5nYPiaUl9g8hGiFANC2VbLBDgAXXfeo\nLrhKyHh6P/BK8yH7SEEXuVeZ2yvZgOWK+pi4oGyBnsA8Xq1Ern2KjTBbAeYHWq/6nhuoV7sQXXjeIt+DDwRK6iHRDyeFfiq+\nOfnkCJSST1VjrVNoHLmIzTu1KidAAYBNS5+eDbvOoAkmA4IJdjZp+9ZakqtRZjSMt60r65b2XPPa29SNzSRZQ6dvsWBLYHWF\nGp0o0bUVAEU4tWcx3f9jgjbNXCGAUS7qUFj9h3wyLFsPP8IouSO66+2QaE2ncw8SZyMJuJz2CHkZB1ABNlVoOFjnuaxm+QR9\njBj95kKp0oNUJBkSsmAsmJRNglqM7P10wVTo05qZSlc0YW4Ob86EYdKDe1g0+7ZQn7wCqaXIH4KJroKvqQuuoDTlQNkjUevL\n64KRAEIGIkpwrh6u0JAWMl6ZdzLchpS3gmwtDuMFLAq8x1LwZaVv0c4EB/sg5P0iL2XwEsY6k0VSvSRS+1EYfUMZ/CImSSHJ\nsrAMJlKFhCqD16uQLC/Hnc7RDAT1RIFM7Fxd9Q4PIIy0uAPk4N0A1bA7iLUSeAl3lOEFCHIkO6ui70Ll/leh3hWYqXccZBYX\nMLxO4JXKE+gA57LWYmlbLKERkQHni41GujW8AglTcKNgKvw4OS8RaYQt1U8qV3K50pSjUiAi15lH9u6ZJBP2UoIYsf3eu0S8\nE0v3nrs9sX4LHhnO4x116sIWqnIsbUI4hvpx4Pn6kHNfQioOFqQ88Y69VO1skmYkUn570Pm0t+7f/c5eobaDKhD4Ee/6OwOM\neVfTzqpxMzXlAu+d2PGX12puTUgr6PE7+GuW1g92Op13XnOtP3mTGUag+eR9rsUMf2czMcffxUyc4e+YVE6fvHomFvh7OxOx\nxIfhTLzC35uZeIu/v9SoX4DewAbF3/lMPODNoN5LYsFfrllSvAMcwoA/IW5MPYd4Vd/LxixsX0kYtFjqtapgjmidcNT4Apx5\n4zrDvZUJG/jtZs9Rk8wbGKjcS44rqd7PffFRK9c8PPawOzX8CCIi3+ZqNmz4C/q98m4xGzcEGV48dDqk0rUi95GS2aFFWka+\nGSk8It19b8Og0fHKSJR5Fu7Nwo/aSmvexgPHqAzxGJDkm8dHDCKpcpmwNzL1uy8oKkS0fcZRd/EP907pbJ3YvIYxkBsJvQ5d\np/N7c2PcMPM00VJR7BIGVqn5c32epBPWzjBh9zVF5DDZPKCiCBMMNMnA3iJo3F4lntAqa4DLjLxpjUDJ9paXHHhb76PehrjJ\nym+r+y5BsJyyxSVZSjpCZRnSjVfqGAyHrh4NioP+6LS1fomGYwIRaSf8KjJPYjlP7hOQm4D75GgRQQIUwyQDD8SCFTNjpVrQ\nYMnJZ7K4kZcV+offAD+ErBhUbFk1kYf6Graey589yaM0zINWYY7VWigyJZAqA2ColithswozTePw4FPppWIsYgRSQCL4JtHj\nXKhrsfTtbinqiiwKLN1LTxU/bIaoR14irSw7HSoNzCVZXBduB2LqQAbNU6a7+WK6DJpuFMQT+ZiCTxDuaDuqlodZPwa2FAZb\nofFFhrcJ5hEI6vAgJPzB5YwHq7XLPh9AYCSu6DJVvO8ZjBsEX3rQ3NFrh2eCZ5fDCo4rwx/Bo8sgwatmW0yOYpHgXfNIJkuz\nQras4oUgwTJD+GXmVGtZKirWeG3yP8fVOv+DXbeMDgzM4XTgjTnCq5lhCS8NTziaObzvZdrwUJRRFjm329twBScKw9jwYcre\nVDSsLpwcPMaXrm/nqBkw8nXhEa4WR3iRiYvY/78umoX9Diz6gO6ZNQ6mWyyFjptBNLPItUzsD3REUzrSG7i2tK+LtQ+/oS7C\nQu53Z+nad4pW8Uleo3V8pgiOGMTkG9WTsIJ/ZRQt0RfIaerK2ha1jFkbMbotuWkM9q3+bxBP6SPOD3HmyEof/hh9s+O1n00N\nxCwB2bFaTWNGvNwYgfMqvgluagGkEa9zJeMvkIXWRcgAUN12XAoZDbN3fadRXGRrSbg7Eni/UDqwJQjECrn1B+7egSqdvQNv\nxiBp2/Q0ML/rt08YaDOZ8NFGsrrPZhY6wuNdc3+YbYjHw9T28gRtuui2VDYeQgOvQrNXrZRdTD1V2ui5cV1E7F5JBQRImaij\nXtsJLAHLFi6HdTIJXs6ePhVDM0tBIoaMYgtBFmExsDgFL7G2EAM6ODQWfZSjPB+DckFRZBgWLFoplQaBNDM6t/ZhYHjXVI07\nwUYu5BDVNQ6/JsGm7lJjMNLIiynqSI2uSyoRhDr6nGQlDOfhlDF5FB2DYAKbNb1iKLEsUE1hZFQqnafUHFdDz47nhlicRl7p\nsLSo25+KMYakaSQ6QLf5uYVH5/NG4orJk0d+jSoQgjXoGneHY/nbeT6Rj48fCIJ8a91lGZGwFDP22puF7fL2ph3QK94TMDO3\narA514wCyMCu6trlD2uY7+HwtpZDqHM4DMdikXtz58AfdVCZIoU4xbmevpUmhAANKBoMMaIM1K3hR+z/iC0zCnGa1GFxnriJ\nbh98s30bs1oaK0M1ocBK1Rleya2mAnunQ5Yuit6iCMeuU2TteFBC3nRlY/mMeS8veNupzfR+pjXfpB+dw0qkZErafny0Kbus\nxkLGHV/PjJVr1v9jZylXJuEPVNph2oLCfW+m/5aspVu0cshyEkXPZOuApLzWXIg+frUHuXy4QXud/IL0Ga7h8xvXmTMjFLQR\nwQCP+J4OWpWK87+BEyRlMl3yTYiJ42zXdMdt7Ue1uv47qDHcdpePsKggxfD3yhzjvdPpFUBf0s/DEV72CoNWzzQd/qCHNy+X\ndDCnMmg+/AEAbadDhrmZ+NGNoQPFnrYvANDaA9ZmM82kYxOcFvOEXIcWVEwi6jHNZUU2DffHECOucmNae/52tt3+p5huscSt\norezQHYt1RD6vgPWoRf2MMJEuDSInj9TsWeRhCo/51bD4S0Nx8ZRsw6hF2PoBR5y1xxzdg/jtcWCbCFRAJEa9WnRwyRoAQC5\nhq6SFzqd1DMvmM5lUUew8ZW+MBzvg8ojT11vnCh9rmJH8G+AvCBI2M7H4UGMh4CcTR0uSdxxa4j9hp/XNHcJLN793soQnkgL\nj1dzMdw+x0scaJMSgEQyQGetRRpD3jNM2nlGxxp8btXfG3Sr/E1+J4FbxysSn1oVPyvKM8HQqF6avK5zPxCp7aVgDz+8DgpE\ndaC7d7DRKM6D1vBrhgatbPgwMRZ8zJgTtxGkWs86Vg/MftRKSJryIdnMPV2Y03nCWVOVeo9mWYuQnC8IqCbiipAie7JaOy/r\nm12gzuQ8HPUm4RHsBBYKzsWIQg3PBSrHfHEV5itHlkePbio+Mqac0cibiqUelB4kD26FXvBThgusSyGrKA/e4D0bK+UJOvKX\nxuEi3BNp7o1gX+6j9zlQxJ2ML4M6CumOlatO574VOhtx1Di0vvKFY655vgqPeiOzG847/43+D53OiF2+E4xV512Fr2ZkQgXy\nTTgEUUdcCY4TRQrEivwRoJzKZFdmyhdH7JLBPxH/aLGEv8Noz/QrXHtK2/dx7h051plHAHpHaLU5apbX0hFMvoBFXsDc6GDI\nMzwyZ0vbnmtVS6o0DkNL8UaAnNFLWT2kSNuynKN4eh6GIEeZzffp5MRxzwKxTLwy9NFGTVxtKu28VpITy9vCe15oV/3bJ4mE\n4sKgVJI1pRHnnpmmJfaSgT+xtraFZeDotE+oIrEtkosFAhMte4oaIGSSCcmqTcg3MLIvjXTn1pxU42IDTjxQtxWnnf295983\nslPHyjeJHlIgoTFyfK1WzNk/WesHaOshi+fJGGUl68+yxX/WHGOjFy2dDE8BRyYCPa5agAnH8OTb4KXKfELF5X58JCBu5TDz\neXcHKD7as+LdZuh3EiPCTKLY7esedVfV5wRIekjX7eHdjcWxj/U2bYVrpiJa921mq2l+njRclIwFKrom8XjxCK1U461sMGMY\n72ZHy+l6RxFmgfpKR6HgwDpFOEZTW1Yit/5iNpvOhZ0Q5a9n3lL5bAtl1yGFdnQIKpThluZeauPobUytyWBOF0edm35W8TJ+\nBogbz9Stfk6uNlEBRgr+JCILC46RCXsKbanZMo7zpPX3JiBgA3lUxm1rpLJ1m7ul8ilyGzEbkTSF5nwKVHmK2GR7UOwMXbvz\nqd2257NNpSRMCdRfhjH6y6mT9Vesll/3eWKmYcyLiUrodWWk+lztX4wbS8l4lRR6fmnmqldFqmSYREXwAWNsBsqokumNzgYp\nWD2VQtk+hKWjb1o3LVxqOZrJW6kI/tJiGtIXm7idxOU0Oq0CXzJvhz5b8cEeQrkXIyYhNBO7SEMz9f8YdWC6RRzINjWQHmIO\n40isRU7Zn/EVEL62QoFSc9gMJWQggYSfcI42dHsNTf5ZuMCrqnpF/2wQPsRk/nwGrAJHUiOA/EYtCmktncWmGyCwAbJUcZZ+\nyueM5PPZosDXU6JSdQi82dRHIJzqHGB8yZA4Iv86mATnoqwKxEfLgeOeg3zd7am2gMuIzgdKVMUiHD4QvnQPP6d83tvoFNap\nvivhOzXSMQXiNJY1pbKssVA2nq5BWR+mYyvckO1BzuEejM0da/kQcI4LNENfW90xrG6qzH/Q2pZMkGpcOCgbtUrsvqsuiXCa\nwnHg5XihFzMBlLAJShho1mt51AMk9GiBn6C5OB2yA+iPRUxTwJEezD0VGHoK7QRJRnXsK3v1C3PLfW3JwjREwb9XqdUqRAqr\nNcYJhrWC4Yxx6o1TUOzQr3j7TSiZ8riPW0o3p88ySopLrI8Uad1zkEjMRZFO+Hx9tEj1aDelY9Uy7Oe4i1ZvxzFGcnsgnVfq\nN5CIxoHBeBXyRQc4jWM/CcfYPWeX1WhHDPIv5+gguNqehRywWcoN017RHROpGcvusCRsvaDTv1UM2JJZXZDWED0HcX8fU3B0\nbeD+8Bf2E3OAFLvE8Jj6mo2LbwjD9XZh+AKFYatZXReGt8nAbHel5F9XzbkhCNfhFJlccx1sfybmgxB6MlUCMYmVM1/MMRCv\nvrlp/o/F4rohFtf/UCyuCdZJJjYe3Gty7ZvEh/89PiIpbYygv4N/2Ky4BfzeKPUI98W4F8aVPaV1TVHqhikKfa1COeHXlrnE\nKsU8pLqgLzPYwSf4E7E0O1sFrKmeofvNnGCcA6YgFsK7WNUgzuwgJjCIyYszPYinTyfWY+OsPxmAPHXCTipXXaBnxCMdIQD+\nnOd4n27bX2JoNuXnR4OjbL4tBf1JF6EhLfdY1xlGEKW63Gp6czTbuxdzvJduIdDXpkRXVbu3fQMWU8dUsh+L3DCMaws3Rr2x\nPRBz7c4yxxavdexEmfnM5stkCKsC9reHIK3rZ8Wdt8UvVNA1BTjK/KB/hBbqA/EzS1xkzUaWtU7kA2n1fzNPBz/4JfYkP7v+\noxyCYF8kKzGdOjU6niBo4mYhqVDmF9Dk5xhj0K/RFtiyimGnQ3ofIwuEP1OUdvR+NcDZxLNxCN1DxyMsTRHKgBsQk+mGVImj\nQJ6RdAga4dIXwHvP3BFsHHp5GIvmM14cT5YlKGzS0ddKfJ1tHTm1wUwqaQuZ67J6ku+euyz+sFdE3myqQnLh9atn6AYMiwyU\nhviHKUowiYJUQMmTKYeMFacb7SsZI1G6Ero4nMNQtTCyN8d3Srb2Jae+5Mjt5ABP+1GJmB06xlG00N+4JbUMKKhPCBJxKE3H\nPNUzxLo6dvT+SoVMtvOas0s/zimKlzGqTNXhlWGV8sFKfKjCjzNLGk5mzjb5bdYwAvpt1vRfRr+pXtUdDn+7PhoODWZfAoKV\nBQX1lvP8Fm18id1G2obmGyQ7HKUSvcdR/qf3KzzTytXLYT6nzBS1ZpQzxif1DSXUSsDDY5dgype4J6MUkA7r2i45MEkwRx96\n7hAq35Kx8n0NzpDtvw+9oTgU1+Irm5se889nZXxq4rTecsJN2Godas7+ULsj8xYfwnIeakX1EMPsF1g3QvPXcCS9IWAj+CuO\nxWdSPA2Vh+Jh18giGP8I71W7wT2/0Y4pv5be6UBrwy2p60l0H1WG7Ia3WXkYvkm2ZqAD1eG3qgrNFStMgu5gyafBqbD6u4tV\neNhTlvx3ypJ/lgcLPe/uDbNPdrJgsjXjVR4MNfReeZQv0B/WKXJaBb/ob2mSpRS34kaX0TvlorMfnW8rBzk/RhO5NcuDvO8f\nHy/QwRoW9A7ZHrSU3lJYXAHOONXHxMfR18I7FbRAMDNQ6vDxcShah35wqsejM/U38AUXZlAUQzZWXxhQtQBHEJHAosk0zOHH\neMjzBDos6XFIhYbwp2fLAQkcmpdOZ+wdC6cSxPGNRlWLkWoxdVp8fEQlPZYLTEMrcdX4vI+JYthF/UYOlOTMs42reWyUgEEf\nhd5SpsFQcEpwuNKVIb49Zo9EnEAYyWHPPw5nuM8SaBWLwR477iUMLlDZaKMyXdH1ekXXXFGBf4bhda/wDqGC83AbdPBqHBK7\nRRqr25t2BKiDz3ht+jyuZsgQYRY+z1N0wlOL+MrbBFxn+e5oQhFQZNrlC/IOjyJ8o7N/Oie4Q8AcjuRNkr0lpbwvHqi/Tp3m\nWJULA3usiiJBf7U5PD1BN+Ku5+pTGjtcWE1scIIH7UAS5SocIkW6oY6HMSw12fp9Fqedzmk3KcWpLy46P0W1dyOGTmgH2GM/\nArWSLmzc8FbAbr3JoY+ffe4cIJtO5x5XW+W3lX0ZTOxbrBdqVgGq8AMAiFM3bMVUIok89acoCSonB2YHp2TGBzVM2b1BnPan\ncgCdh02lCuKXXEYlqILKqBS66N2Fp908+w15hZ+tsRvUfJt5dwjsKK+tDcCximsbt1QZfpnB1jzxezmUP1F+C0cZBq2+QXC/\nwWWDFrFJnF7V6pk6dn58zOXjY4GD+lCxa+2d0wvBtUpd30an1PE13lN7DP/fOnCioP+605njpvNhhrRo8RlEi88vvmrR4jPI\nR1jma/8zK2iOWfX1OTx2FbCHsFU+A44EBlYfqsCTCVmBRPbx8bMTloLIrrFbk1AbcWe9t9AWLLqUDggQ06g4g1Ica2c/BP5Y\nbtvc4Z5mVrGrd+FN7+7FUI/ozkp8p+GwfzcIb6OfMw+f/OBIPfTuWfA/Xd9Z0ObDepOWw7118DU07egrb8QaLQaiS3vuFCnt\nzSN8YwrDXlIwdIHJ2sxAnEDta0YHNPmwjJeJd43KQOEVMjxpwi+bZDIAFxL6fYjQc0rQAo+Q0HZtN5Eg6AqB4bnDM44NpgSW\neoO5MAcUdzZuHWCSPWTIgDO6U5yQd9FNskwWp1dnbxCAzYuOlHXRRWMTBTlYwnlVeBc1Bbd0wHwXvfQ2uiLuAApwfQDvACL1\nYV8HEvbSDi3cLe+QRj7N3c0Bnz/hAvgfoYELcQLFjq2EddMBRu+iS8d6MNATfkKEcivUYR9XrnKw7huMOUgF+FBEXHTpAcrw\nL5UxqmhAG4ff0EJjeMypfJGbsHNTaWEZBPNcEr47kuFFfw4PFYACPvS8CnHlEcwAi+/GMwy7NZfwBZQFYL4G8IbBI8FyeI1D\nh9fAaXd5DZyYlgTMc6f5osa8eQ1wZPhCxHbq4DTEW9+GTGVFjEgMmYGX2/aenqBbmJ/bF4d6dm7t5AA9698OAAQP8ec0vCGy\n7N1oKn+KYixw/kAwoXc3bmyef//kR7Dw8IEfXANagCKAFQxlI7RAinbxcRPBHsL0Xfv6aVw5gt5npGKHPtKtz3QwQQnXtChD\n5DgB4XIzx0i/1j681orozxvKAtht1/gt4CL46Ulc+ttO57NLLbmJW+w71r6y1PGaMzV1PFSE8Vr9HuNAf/kWQ6W5HkaCzOjk\nHm7TU0hj/o3SFStHeWuI8mIDUZ4IF/8Tk3LYQ6jxbgF13+pjfyCUkGd4s8hLgFASP5kAL08PwJ+4vG9/AMmn7hiAkTnYgx0O\ncgMgnn8on5mrqk/UY+Rtw0m0JTQZEdCRL/KhpVDecaeDFNSQVICCxZQmmZQcO2q+G31FJ9FvLcRhg2SGt2ZSDpvxjqJjdLbs\naqN6I6Kh9PS6ydtCyl/UGrb8+u9I4NAaN4a/zJB3QFQA4Por8JNIE26pUTa7kUV4hZHeF4A20OETnm+7FKrllVwwkIM8BoVv\nkhIYnVdU8BOVayF82R1+qydP2a70VPB7xWUBIUXrpFmeos8V7melzv8EFfJg1FBW4i81PDuqr4gFzaicERL9nanieKz31fQe\nNrV60WGk/KVhD8Q77yt9o4NDUV++8g0vh+KrijDgKdpjv/vKjFIIMtWnv1sFHehoCFy75iiRc6M7RWGXjWrYWDXsGH3knmuz\nqilJAfrWCBnOoRJav1fSX17A+l1gZ6aSRzEEbA8t++KVNCPn6NHUA43lh65F5ePjncdcqY07jggcqNMFEKPeJbKxSJEvou2N\nBReQJGCD/lx6J7CZkOQxa4TkmR6+zQMBtYOxXoCU4AtuyVw4CxTzKEXp8auE2db8rXkKgUree1+JWsJKY6d8MZLwBPCFawA9\nxu5W1F2itUwUXyOEVFweMdcHRFdI+P+u55pcGlK51vdjJr/Ejl8oOQ8Q7okyYSM8KZajOa7sHFZVLzQQfIqSgVQftT8VrjeO\n+BTlZTv5KF/A/Oa41b4iur0Ise+F/KZ4dIHMxKEzq8sjSQdl9NMdzmK802qS372H9j3fpLPTDWHKS2SJgF/B/UrdjfQD0epA\nmx4Y6LSLQyunGfdXkjUTvCq0fV7h/OO50ZRXYEorsH1oesf4dhsc9uwy0EBf8Rp4Lk59/sOPyGSBNFd4c2mCK+nHZkkfqUlM\nfYFf7IyzWVEpfRheh6ziXK16Siju5jbyEYnpfAOUupnk1rflplNTEBURjFAA8xW1cjC8Qbo87P6Zjzj19fQV3uCiM3sg8ANg\nnGKoryGF/7xx44vCTPwZe6d2tQVt6ncO7mzgzJ7GpEPXfro31PgMBk/Ij3keQqnqnPIr4u9TTjHaBUiikMJz2q90Y4/Y2SoM\nAiw7vMnQ5W5x/ABgViuyJy4cflc4fMmJozthFgSPY5TEcEIqxmUpgeW42NAIuRieCuPaAx7+u8KoY/gp8k5Jr/KARRm9oHiF\nbPi1uAA0iNlRgSEwo+1tB/pTcs2F6n7ij5Eno+9IabPxGcxl9k3mYhgOHx/fJACdh/jrgBizP8TsqceL8CyuZt15glqLU7Y5\nOSFm9gSY9ZMXF70Ty6WDrHDYPxmENyiP4xPJ4/QA23oIv4KkBMUVW07o7uA0erBaemQkLvwglhvaOUhGR5pvjYy1BfoeCjsM\n6vaFGeDuPojip7v7rH68exECUYS/Jz1nIKhFECjLoYJBDehOD+iOVSkgcBSIz33/nh+2jM0xN0PNxepvm7xQTX5jDv8/mrzY\n3RUnu7uINe8OLgjWsVmn1RMMkCUBfb44jQ77hRwg0//VTA30UGHkb80E9CCXbvsCx2l2y93BiW8G3fPxMAanVp/HQFlX4wod\nusPu3AGFDNV9Q9SXO5COuT+O/ufmb9bnRloGvdMBBgRPpzkRhETFrqCsvWd5h5PdXD7dJ2j5SjqPVzb7jXRCegEronq1B52q\nJHXqjcS+7KmMQvKIm93FsdNhvjwALgMyJPRJT0ZPS4LUu0PSDbnD8A8lclI3ZiDO9RukPujNJc7QXPLdMtChuYQhYWAekIpQ\nPgbyjhoFoItQ11wqA4JDaUx3okaPAqzjkOu4A0A5hG6/khFSUxngFO0BP0WVHsrBFlCE+YWuqEP+lzL8KqMPM6jTDwDvqG6/\nlHZb3sGkwL68O8B53d11Jg4W5g6ah/W+gZYu+OHp/kBUVYgPAL0XyB+A9D/1LqCBrz1eD7wqT0Mw7paqauBX4h5g2vaApwXc\n/BKnbBDVgHpU4ec+MFzQFdRW1i7m4SUxh9Owaz4zPwZVO6r5W2tpfeOQoDulqb/rACmpSe++HndWH8jZwLd3TKaG1gYWD3q9\nLeV+/N5fSpXNPb6SbgnJWhNACN5nFmO0EuYCpv7ixY3GnBewftC9G8RNqp3E0wqAhgCka32V+8sjxUM4eV9hdp8D0kP11C0J\nWnSR8G13gQ6UIBtOQN763D/MB5HuUwCSpqt0/4yqAM4SmpdT+vPPyMq5ZzdLCq2N2giZxg9v6OVExFMozC/IA9wiqlHiDYq0\njogTYZ2Bbm6F6Mi5+w3A9jMeCGFVSXbz+Njirvfc1E6H0vBuN9Ec5lxGOYiGwQVUT7Xim2AOFiOCrnon0QnkAV4FQQFYf09b\nMJn+jC0oIqI6drkkPhT+rOQIACQ8HW7A4aYu20LmqWgoc1DHfSLGaJn2GgT+e5Q453Keg1SiQBiYbzxBhw2Ep1vqiBr1BNQp\ncWtioxC/97XwjAKX2U66PQL6aMq1aDVQRQ0/dNBAR+SOnw/SKeO8ecr82CEtoY0wgMJEI3AzLOEpQt8JSnatUzxlJFR/RIgW\nmX6U5lAZKtdEJAUVRvxUas4f/cvGxnWOfzFK9cZm1e6g7m6dNk59Do0qn4u2qZnvIz7G67Jhh9nQeOyLV4HfOSYGqCZEbajR\ng/kRsZD4BfveBN5n2vuY/d1P3wPsHoO4gypzYlSVuuVrp/MrTNDK0MbcrA/QaHYD/cacWcfLx8epfHys3BOwI9mYxI3hm9gR\nMHSMGObRQSaHkCbA/zUcNuH8UADqvdany19d3HusIPRQITuQTNcw5aFCWCMUQo7xGNLdqMddizCab94abH1m7FB41//Laihe\n89BVWEOJZgV+A6PBdnZQ2u0qPAYRCRuXOLjPfu82uqVJAyIH9O1Go47PKGCljDXWD+D/7vT9Uq4r05ajeg7zTFJqcCxA/ARc\no2gWdg9yb8UcsEzMVO4qRUkV/t4RWP1cel9hkvTFqJ8j77MNHY3XnUmhtHHB0PhhIHzRIb4bCl9n26Tm9+KW4O4WVRpb9VjA\nv6wEDvJhE5uKz81TSKBtn3tS2nNIiawNsbNSDsy5wUqMJEHo2rIa76fRVlrvb4AB4A71hYNBUMA2SoFDXC9FiRFZonLqOjzs\ndA778cxYZ15HMwDK4HBFuO69tMEQXrsrSzxvz6qZhzS16GuAOnd+401qbiRFnSpnuOHv701xdTsVNmE/ZMyPGh0uFA7Fe3Sj\ne0/M5DxF2KAbtainMJ1XMlwugnuEqjHqwWogZ8GvYl4Fr8V8jEHzFuNgRyxG4+ClyIIRCEVBph3Xl8pJ9LUUs4cJBtgwkTPI\nZOHlYhHczbzXjTuo3+QeY5ZMU9CVG7xRG5t0OmSaitg6ucnYQ0e7CipDE1UkzrIcjeDybPeeUukSAJmN8wmxCfbZCUk1q9BS\nRV+J64QguUy8JSuNoIO4+bh7VeSZe/m+ey6kvRMmMHcNhrvffSfsdWY/OEaGXxqBOTx0HM4olC0AHvaM/aN86ntLWuRka1hs\ns26314egl5l5URbRCYeIKaxhNF8pmrhXihpr2ATj6XOY5wIf8ybWbOWbhz2el9tjebrdNm/Y/X2Hty96XF8IMiT+4iU7aDtD\n+wmN4J1PWmwquEA3w9zHkuoAcpbTZVoNm8L9RtVc3FRsvtzJqOt0nmnzHXD8MGsG72IXUYy5ibelqDvSBV1cZu4yzdz7L/lW\n3ZSu011qC+5MRThDXxTWgWEAd+eaSoEmyy/GIEagUXAh1M2UiSOl0hWEIdre2u9g9eKen4fl0xgv2cz6GI0V6onKMH+6H8Qh\ndPIFppYD8rNANsWjJiAJmsXbvGDCEpwBql5Xja1QiV65u/vkYK/nU8EYjXQxoNZmrJ75WtSzjWs/2EFI24e75z6yee4TyWCO\nRZ0oRKmK35T5f3P5JV56aWibcwnLVH/snmnZ23ScxJ7tvu22iWN1gybumoKQSZpiR4ZTe/HQpSIezk2Daq8TGmrsb47bFEl9\nOblxCMErV5uJkEIXiqoWT6u1EPXTG+ReZ/lacnWPyTvZWvJ4jsmv1kuXGCVfnCfax+XXau3SxM/QX0I45+rWPSzBN6/gRWA2\n/C3GbcIyxHNAofOkb1znAaiYVpHe5a8i3LcNvCTfXmrir+JpmInsBUDsrxUhQmjNNcl1AhdOXa+HDePev4qDvejXCrWvKogQ\nhi/CVK7618retepcfyibblqqemjrvpHDDvj29raZzXQ/+pA5HzU/uWx0P+KbwJOSgu1QLJ3A9Sp+vxY0TGM2SQ/o/fNFPtA7\n/CpwOZuG3hJegVRj5PQoYmutElJRWM1QZB1iPgmvQwAHIIEYo3jzNgCKiNluP8XJUkGVh3w5+Wv+4bBuyyQooFqo+gtUOg1a\nrWoVZAGHkbILRzPJZxjqKAPQpwpOJcowI0ki2gv2BTsZOtQuDZfOPGHMSx3UHZ+bHIWgsXU6Z4hWSD7HMye6dFPZzQUf66bd\nBMcJMpdoOY793Dftb81v2nJPv2q7PX4nqZ4HaaUlNr5n01YtTnGZuKAQuvb5EsPjuAkvneKlMvjH0IB7jmKhdBQLiXBNpIJi\nQyPBnbCRv3n01T0s4sqGkvT+xDCQFUwaMswIahlGXEuTr+RHGFR0WY7pwiPeLlT50U8Bhl7i/daKec9BOdO9A2AUyg4KxGmD\n9uOtZHpvUphJXvgPWfjJ8eX4NPt7GFI3pzCPBfB0P+OorjsAwbTzrNfKkK+lQt2wvQISxpzjmFX/S9V/lx36EbBLH3qaoMaG\nPNAHYR7orjMrYnmV3ecip+jOCx1glnb8eHyxqEqfKEX450xF8lySYRxGLMGTXooYgfeFtGBmc76olwqErxJqJ634KqNPMf3i\n1dP0nqrwbhjfRLIhXfgVy2iCEqt7BPeDId1pAesb4DV1fvTj9wG5nEXfB7y1nwd7enoamDBmXOjcfjdr4LVPMeGHlOrn7uAt\nd0xI7aVHheFrG0FEtYs7bl4XtBuhNZztpS8Ai3ZmGHwCg/z5AboY/2eswUbc3OCYUMeYUMfYoA7UDU3RvamMdBQhZVZFSMUP\n+qWgp0FAP9BjjWcyY6nbRDdNq113TAoddPmhiRK6ztsabui6rw0skXWdNwdjOJfsORMs9UXIpOqKYr6/fP/HIH7EP03MkjVs\nMTfRzIaZl4tz3Hi2jDL5ul13VV087KgCLTo20ruLlK1pdaeDAObeDugia8fi2hQ0NwMKh1EMGmwjoXG6h07PvVLYIwalAP0C\n4xfifSqOZhcjn+oXg2RBLkk6nZ3Yq0WqrifESGa13SAvEyTAT9qA8/bM7gLuYpYrkb9xt9ZvFZFrxVMZVtb7jDIN8Co7mfoK\ngJYMwfSrE1Aqc7exNjt2OIORcm+N2JIsYK4WXk4rVZuWovyAMC6eH2bUnu40e9LSBWam3Z8b7aKspu2DsqaIuI+XbqOSPMqC\nYdFw5vuzdC5g1IeLTuClRF/WoiylmY64d9TBbq7C/R97a/cjwQftnJUQeLqSdH78wfXE1G74FHquOxzzz4Q09NSngpTwbs6e\n3zB9WFZA/3qub2erQP/tKYfGRZfYsMgCDMDyXadTULAW7ZM6JKdLKBRiFDL4eS6cKXsMKV5QI5a25BgfPAzqoPG6lNbqTFpf\nTCmGij0Q2FHltxmqdQS+KkFFuodTB7DXf4nhygbAIYQ/9aySIpQic9kFR5rcUXLQPwzV24iws+4qnPgkb5sAU740JumJNkl3\nCWlfPQuVO3Aiixc2XI/vENK+eoZP6Hdgv8hy1Lg4Qc2LgYg5pFDMtzPHaM98zfHEnaAXsY+3chaDsIxsEOFSxH4Qg2CvtkKp\nHzCsFcVqpU9itboY26/d1qnYqL/lmtpbI5soj+ghJ/y36EPaQPMIv84o8CuJhp8bcWZ/2biiWBHRIvRk5F7W3cDywA38OgMW\nbYnhYT9jeFgd14gIcWLDGzUupeFLqwixGF5VK8SZq1b6OnxW4STpGdV3XJqU6WQENqEDQ6W4pEyOkscV8Z0E7stbm2vD6Onv\nOMYvjvgbAX8LkwE7ZMJF8bff5qtjGzfyUK1uuL1mkGEtWugww7oTNpxfPaUAUsINjxIUTqIaGIebdCqwUUCqZvy+pHHfAtE3\nKELB/8aVZpkqFYcIHth3HB7oQrJx42Yt/daQOWwcK/MITFMVVVpzAm97QuuOlNziKo8w9rOxCuQXcwDBr85VkZgwGitph39G\nc/5VP6NagZH6McnqYRKrPqjVqm70g6pPqt+SO2sIfUmnrcthUK5ESVevhQgMBNvALVJIm/BQX31GNWCMjzGxY2PJ9y+SfVjl\nRol9x/fOn1SPjwX76++U4lOh7ZMzcmcHjEzhV/R9mvpa7oKD6mHcKnoit+hCayJhox4sCxtQsRGtH0MnAH/d3xt4eHNebwcw\nnddmp/nX55dXL88Pj4aXR1dXR+8vh8M2EA3oYwjs16fCFry8fL9W5s8Cyij0I0uKIKgJwonRzO2UpLpxDD75mD9zTTvFDjLi\nq5W4S/nw8ASdpar1Inz0aTHbUVO1tG6W+j0twZ942bL9Ri6UEktp52HJPhWevlV9S9TAlY6nBVNMDfYoZFki8LIv8RWfCwEr\n6lzMXkYVteKrExajpaS2kM1wIg6pokt7AzdFTcncq7C2Bj1T10yRKRZhOo+52cuZHgztYGSjOByE7940WqAW2NnhYWKBJ0Hh\nV+0MDCSUYaSHdwVeWg+IsK9j7hR4PhAOayCGWD9dTVri+ubI85ULlHFPC5Kj6S4XvJ+MQx+hSf0d4OgUJWoT8JuSU7wQtUC8\nTy7F71HnmBLXrPyGeQZXK7wvt+LIsMs0p5C/e2iEnxnldRgznaU6NNF9mDZ40PcmXiHfa2Du0S3L4nWWJpl8Tzg+Qmmj4OcQ\nxYVCP6orN7zMuaU6PK2RaxNrjT1Mt1Pinq6O7tpRNScm7SXAnGVfKTorLSTOzxnHx1BzQ0tQkC+1QikLvmPRVUrinX3ObX2o\nmHdvR6PFt3ea0VbV1/9UKKubA0V116UDfhyvLFv4irboKy+ZnPElleoaL+kc69AdnY4wwa1F+olqd64hersG9jDZHyf2kiKY\ndxozTbQOkmXPNlQgL0o9MScNJwWk41TSlXyNG+roc5Cp+IPViu7Xoa1nR0DqIv3FCV/40DZ6I3RG09YRdaZj1vw6IWj4E8h9\nuQjb33V/6H7/XZvIwijW59ppqGSaOyA1+d2Ldg0MIz93q6LGY8crKECBSQ9TH0FiFIeHqUIVb3OQ7R689i06wC05Df1T6YLP\nlc+xc5eqa6NpOIojyBnFXVuUUAHeh5pDJ2dVtQiePbu7u+vefdfNi5tnz/f29p7hGTCwgdvy9//975+eoeE0/Tl70xYXmR7T\nJB/XGE4FRxXpF8Y7b9PwAsSmi6zbCNbitSs5B+GeXHtTAG4V6MWGx5FdTmJrJUiv+DwDVlbFgnHplL5tFfVNPaU7wkKkAiFw\naMaKMVH3TB1FaEMurPf2/NLLF6hHk85B+LZSMZWqNrJQ1EtKVPcHm1lGD8pXOUu8Iq1NGoqkOwcGMVmk1k4JkROyhcmoBgLS\n1vltYcsC5li5oXAwhqduFBNwjugS5EaAHLeUSsNCOmqOjs+E2uiJpJD4oVyth9KxpVz/aCjnRNjhW3bNeyPeDl9XaxPEX7Us\nHi5pSlDlQ31spKk+Kp0e48esOUmSQi5ui9yzPUQg8cGFvE3yutQdk12QUdkbiALYUbD7MCyBAXT7y4f+vTXYLVjHRPOOchAw\nZC39eYuC5jeq6Cl7u7ep44c+mnK0PoLPP17Az8HOMlu9wC178AeF9zWA+ccLfFL59PhHkNmbyd/S9auVOiw2tXIYQBNPxBwA\n5d1pUuix0/BSN8XPUdYECsdbzc3ze3ljG8LeXZsZ1PkrUbkfR7E7DzDltibxH9YECJ8YL9RBKzC6t82byOvFZozg8QLIB8rs\nKDz36Z6FZBD06cfv/pknmdd+0kZNfqhvNeWxOLtPaToCjDnb3JfK7x5PsFm9cY5R6qQO0506Xc3RLHDqdr6cQcrEpuCZ8yJ8\n5kXB/zz2/N/Lf02SEnDnAzwFz+woF5ujVPqSgs+IdGSxfRXculXwXc0+3wonfd+NuYv6N2D+0gSQdc+99StWikaQEM1hTDuA\nEupWiF7lBAg7R24zpx1o7PobV4zhzWGNwjEWXo/+C4OiO8jUyFHZUtqIUdApuoAsV2elNwu61aw1ZJYORD7I1VZSeMutaij3\nA9ukde9Q09IKw8q11JksSJnkVU9DmJCnsY+qq7JExBeCIB3OFoD18JpbXZXkCxbXgEbptXpvKHYq3brYf5OiAirpquEF7Taw\nc9MFxejUiWE7AyTSNodJkzJ8BhDQSuaLvKjirNpxYOG80LDACtbK9ysjSSbhgcpPHKVbpVcBnhDkmheG7O5Cy5NSj5AB3rkr\nsTJXLUxKYkfbpl9t3ARrpRshiZJwzrJT75sN/JagbPwfGiGLG80xv0rDfvuDHH1JkD8+y7/C33nZHojrnNScxqJmTWy7zvWd\n25ql5LAmSUiXlhDKRH0fX0mLdJoDJWqmHD8Pk14SXtZoWNSMfP4q3Rr6/FXaLwZPKTBouaW2cos28WZhAwdbI4X45lxFw7w6\n+nj18v3RS1JLYsJdMqlm+habmUxuZhVG2xhyMHS08NOhXq/Tb/B//352D4j3S9tO3uvUJaHh0YR13HRdtgM89BlgiKj6Fi4F\n3uk6NTfQ/Ci0/4yvYGe9IAY4rPQpCYZcPaoZaNa3m1yvAKCvjNrt4C7DD5QqHWtz5Lvz1DUv0dfZG2KsZ9HhcYBeVva+VFy0\ntU+i0ZRbadpYY+BbtWo93Y4JiIFg9vb9xcn7o8tLDCflqK3buy5GLunSk7dXry/O8VKimwYpUoFVMKwaTAWHzABpxKyEDio2\nnsnxl1F+347aeQb4vB2Yyel5gFnDFLkVIE86LEfG8TXVZacpWu+Zo6PNZRBZlz8NtUpppcNa9xj7UChiHU3HjE0fgeGuxDiX\n9iCsCmnRA9NszscGFC42IizGMagDyrCmNRXZ9+EpOopZtFqVlqDibb0vUCtkwSNP7N7L8Hayo1sAgTdoOwrL7XGOc9i+cItz\n3Vu/UPH1XOZAJi4fc7uwcNmYJgzGepwOULbGX4zcjRc7KUyGF3j6sVqnxMG8/VxA4bOFxmvWlJLuth8t+AYyGm8uxiJVRByp\nII0qB9IJC0+lleuKxr/3yLGgFdnjW2B/klv5qC7F9HeeiUvI/J88CyLgab5RxrmcnWRyMkcUHDO856F6bU5qo/sFgmHrUhFf\n4HN9c4WJyIxt6Z7QR1m7VX9/oNGLkH16bdwEhBdCKRjtZ/3n6G8H+yHSVX3nB0COMnPrEFQyIFh+nRvPyqNFCLRrnpQoiJLO\nHGPoLUgp+hqjqx85ASnwO1/A31cge4F8def5zqKP1ghUwob4rQT2U1X6/ON8agg657wIK1TnoPZx7dYwvNaLYEJZLrsHaAm5\nMrwGMXCSoPoB6HB8Q+bevW9nscq35BjlCbJHQyy6YKcEo1UtjLlvHqLFrolmD7OXvojtJbr2+17q2vniBVK9MV0Qhldg/SBy\nzXENM4p++4OgeL/GvkiBfibsVIQPaDRZ6ejm6f8j703b2zaWhcHv8ytkTW6GIJsUwVWkDOuxLSdxYjmOLSeOFR0diARFxCDA\nAKC1mfPbp6p6B0BJdpxz73snfiI2eqneqquru2vhsqWTuZ8+hWPa47zWRt8CruvSS4COdnl0uxDdcR6NBqW4h26ny46WnnXO\nVDcOMBLy7LUX6IfTfTqthPgEGeh30H3i7smZM/d9FeLfT58MkpA54xq6tcJCLSCA2jOW65CSlEz8VynRdcaPOSCs19mviR0Q\nJvGB2qDs3UfyFR9N73i0hwRT+cnvMlDv6dtvBasQkgSTYSaRdnhphxKAXnB0x5vn02kwa52e0jXwi8Sfko+5nePHzfcnO3y1\nY74HxMJAo6nNxKSJS/0xb2GergLtse8UP/n9RcgHeOYD6jTlrgsL+5RiRB5YmJEeGYNxfGyQdSKewjfNg3twCxAlmP+fIzqi\nfE8sumIBsmUQRTSYsvQ09c/PyV64BIfyOnSBJiL8VZ5MkjTlKhscjB9PcUcn4SLNGz7/7vXjw2cyE/pzleEIdqRS7pev3h6p\nWmE3LmZQrKYcAVf1o5Lp1B7dTdaH1sLzw+8hNwV/fX7w7Gf58fTxy18fv5Ffb35++/rpM12dZI/FWHJ5yAfumI+x3ogvSi80\nhGHc5geS1Fz3QJ5MONMvKiBF+VaYCYV54Ge567DUewT5UiQNsD5NX0i5U5FDEJ2PqVe4wsSGHAP3JRxWG94nYWuHRslTAb1w\n5N6jJ/yGexwYaPmadkspmMaltzLgiJAEa6mfqOA8nTLvBTo7F9bQhV0W0EEU91xiXmp4b89DgOvLFdrVlz07ijULgxv7eYxX\nGJmODOMwD33RN2Ove5nIY6t+aSPRTX6zQNbW4TvDdwaUI+fVLTNPXInjBeDNQrnjvIn8axS3xtJjFJNCDnAcrtfI1puXkLTs\nJSOMCxUoxfGcHLEIWRxqqnYnePxH+kd8snOOp9CxXRSA+kRHbgWA5feh/L//r3+jP9v4+CgGFiqtpdr/b4iXi8IMkwioigQz\ny1mzYB8Irx+fB8Cl8KkA5u/RjV/CAXQ6CvXUcJjFSYDcawGnhEbzUTSGAEpw8oWXb5+FUlAIGS1RgFdBwg948Ntmr5ewvxfT\ngni6zZ5EKkVU8wRFZKXfdpxCqgUmzpjK0hwakmD7cKBDQXYc8T1hkp4+WKac/pSnGLeY0sQ5SNyoenRetF8xXiHtWHxI0rXl\nYt5sehJNacrHOSsiZMgRMpWdydBoLFeekWjgkxiCnDebb0u8Wma2XeACcsE7/2r/MRV7o2ijs5+p1jvGAZAGjbzcAIzIrmHi\n0SESLWLRpTUcACdb0sUvHMYOxBPPp09WtLajBW2ZCCdk4nmAeyFTMq7bKc083m99+y32AkUp1NCL5U4No/dCecSEgXq6RPm4\ngCSJ9aInmqFGLy8iWK3g6kJTVjhIfcMfEUivj9gXdLqNoJRGnz6N/uBzuZ7IS0gokq5syOMLJCirj8atGoqyR5p15ffLexO6\nWJ3gBTgKEdQmxu3ju9iqUTqZD529bD8hX9gpnGhbHM3R0Rh6ZRDnstoRvkzTilbraXwdFZG0NFrX8vXcfECmyAp8XvODrDGI\nHncOk+4pac4URwoF9TjZ4tj3CMbL6CVlC8g9uJ1PmRsgxJComXp/QRlGPSTVHjVf6LmPlrf49lJ1lk6qkKWKvIilmK+rercZ\nm1KP8wRL6Rqvxe8FhZ+mViLdZXmPopZkhx1y8gJROa7Nb8hz15j/EBrKhzw8wu+/Q/9hRmucfYkRgAYpCQPBOQqyCNEt3uTj\njJHUvO/sq+MW5PZP9viVB1I1X8sb/JbVBKm3wXhewjliK1ae9FF4Q+9cYSUJBzL9QyTcn2wglaTqWxxxAzvXQrywANbg4szW\n7VW3lgF3h27gyFmPHmJgq14tkYk6RrM76FUQSJdssLEWXi2N+3SEROLX8pr2r5hfX3O8jh2hCJmF10jrgoI7TNfyl4rPLA53\nYk0rwdHZbvF6KZpoDIJCmtCjJUgMLb3yAIV9wJebc2NcRxOqCfxUt9IPM/Nm2tc5UKA2QVLJZYRyx7wvUsINyV5k3MF9+hQZ\n+9O+rxYAnj2RK554j8Qd4wSJvQgnsBqMvERIEiQegsiZYHDIEk1tYSKoieismZ6BeUaS/ldUwooFWqHM26FidLkQ6TwXSzVd\nU52ZdhDBspoXpPvyzlPuu4ay7cyeQODh9MkU+DjjDLq9Z0qwANDj/GSsnhKXpHSkveIcLdcsWgqrdoZW6cGypgVbIthVv4+8\n7+e1F0vFtb9dejUuD26cSrBYSxkW4MnIYqOKS456UkrVmOLodkqSxpdL3LUQD1Obv0Bv9Kda9/fB91ws+0EmRKUoKAVE8KlP\nfXipfhNHqUkURjjifJBLQv6m3MH2thblQ8+HcIh5vqTdWbqfM7kXwagQlNKj3cfmJEr8D2ihsPDSi2K5zY9Nf7kkMSwUCWQo\nza/fSJZSM9rilX79XlQohobfA2ndBy5lo7inbQkPT98mICufhCUe8Y0XDVOuiSuhSTmdkjzFWJ6rfvG9bXfQ3mbfLYFc/Bp5\nLvt56XXY4xmEnmDou9jrsgVqAv6VQ9z3McTN8HORwueHCD5/iyDLu8jrseul12dPQ89tt9kPS/iBAvjTYX8B6HaX/Y4/PfYr\nQIYsH/DHZb/hT4e9w58uO/Lhp8ce40+f/YWRA/Y7/gzZn/izy77BnxH7CX7cNnuPPy77EX867Bf86bJgAT89li+gpfECWhpC\nBPspg5amC2hptoCWJgtvwPyFN2QXM3REjvkmmG+KHVzh54w+F1BsjsWWWGyBxV7PvC504TKFH5e9wZ8Ou/Dhp8te408Ptkr4\nGbCnvucGXTaNcVhc9srH3w57kuNvl/2JQwK5f07wt88Cyjdg5wv8HbKfUvzdZXMa1hE7xXjo9kf6hUM7loeOP53hb5f5mA+6\nHtFvn73H8u6AvaL0IXtBv7vsO0zvQA8QTsdlc6y302Vv6LvHDum3z36mfAN2iHA6Q/aM4nfZAcLpjNgVfnfb7C1+d2H90W+X\nPYHed4fDAbvmgSH7gQd22fc8MGLfAOb0d3tt9hMPuOw9D3TYjzzQZS9hoAfuaMB+gZhhb9RhwYQCA5ZjYBfGJuaBEQspAKOT\n8oDLMh7osIQHYIB4AEaIB/pswgMDtuKBIZvxwC6b8sCIzSkA47XkAZf9Cr0YYHsWEwr02DkP9NkZIMygs9tlpxMK9NhHHuiz\nSx4YsO+gX4AWT0LEI5cdQRFcE4/pF5YhYuQFomBMimevgdhl6fnZNvsp5qEmirz66TbLIyU10Vx2t9l5Yn6rbO8zvP3j4amC\n9iPEpsFk2B5ts18gjAD+DL3hYLfN3ky8PnT9NSA9Ihj+dNkr/OmxJzP46bMX+DVgB/gzZG/xZ5c9X1C5Q+hqv93rsWcTbxs6\nuRVk2+wFon+3z34FQgLdDxKkBO4eXYxvXaU3VW9tzk0+D7PWaSTiMs848heTbtaOutorpO2Fx2RVUpWlbxTOx4CSdMkdYQeF\nYklqH1hU4EMqGobUfEPbyjeNxeZIx41QjXGJUWgL8SfrDW+K92mAfguy84mnw/RB0f9tRluwrH8vExxSKk+0GUPWyL4qDD6j\nJWG5JXRpcCIlL2RzAnG9RflVN0L53qdFL7g5l1TZ33nokw0eNGLDj2kIANl3BZFbt5YcVu4db7dhF9xuu/ing3+6+KeHf/r4\nZ4B/hvhnF/+M8I+Pf87wzwT/TPFPgH9m8MdFeC7CcxGei/BchOciPBfhuQjPRXguwnMRnovwXITnIjwX4bkIr4PwOgivg/A6\nCK+D8DoIr4PwOgivg/A6CK+D8DoIr4PwOgivg/A6CK+L8LoIr4vwugivi/C6CK+L8LoIr4vwugivi/C6CK+L8LoIr4vwugiv\nh/B6CK+H8HoIr4fwegivh/B6CK+H8HoIr4fwegivh/B6CK+H8HoIr4/w+givj/D6CK+P8PoIr4/w+givj/D6CK+P8PoIr4/w\n+givj/D6CA+5nO0BwhsgvAHCGyC8AcIbILwBwhsgvAHCGyC8AcIbILwBwhsgvAHCGyK8IcIbIrwhwhsivCHCGyK8IcIbIrwh\nwhsivCHCGyK8IcIbIrwhwttFeLsIbxfh7SK8XYS3i/B2Ed4uwttFeLsIbxfh7SK8XYS3i/B2Ed4uwhshvBHCGyG8EcIbIbwR\nwhshvBHCGyG8EcIbIbwRwhshvBHCGyG8EcLzEZ6P8HyE5yM8H+H5CM9HeD7C8xGej/B8hOcjPB/h+QjPR3g+wjtDeGcI7wzh\nnSG8M4R3hvDOEN4ZwjtDeGcI7wzhnSG8M4R3hvDOEN4ZwpsgvAnCmyC8CcKbILwJwpsgvAnCmyC8CcKbILwJwpsgvAnCmyC8\nCcKbIrwpwpsivCnCmyK8KcKbIrwpwpsivCnCmyK8KcKbIrwpwpsivCnCCxBegPAChBcgvADhBQgvQHgBwgsQXoDwAoQXILwA\n4QUIL0B4AcKbIbwZwpshvBnCmyG8GcKbIbwZwpshvBnCmyG8GcKbIbwZwpshvNls+4R9SLjR9FfPd1zY+w9gn95t74gofdbJ\ns5qjlOAoNfXjabKoOfVeZwRc2bAz6n9qs+C2xPy2xPCWRKlh9yI/jr/t9PsnDQw9erRrfrgD86vT41/bzW2MCVRaoIrJJCrq\n9j8NejJDoXD+7aD7ye3sUnJeLJ9bVee6NHyFOmS2NrSKhKqIY4ur6FPeu7zwfMYN3fuXwA0om/c5swwAvFxYRvLi/woagfNf\nxk3Fb4U3uZrbzJ163MjrRqariT5f1uJv4ybJSaCUBNpi0/ne+sY5lJq0TC5qHd66WZTAlk3BKEE7BRzBXrzsmA1+KkwzCU+w\n+EoJ+Jau8PQqvMJ+B2f1vNuhq9mxlI7kvl3fhnEpZUcjkc7kDgqZBv1+10jfLSTDxPDE56UK1CxALrc37O3COWDIYIhUgUJl\nRoFuZziw8+5uyup2eEZpPSCfp8nFFj3TpinaHnsef/QjYJ7UzcsW3Vhvm1dZH/KvPLp8uSYrVLA1VquzcaStAjTozqZRt7LC\nBDi3zoDKqObg9gmQ+WkKbh1/mRNm4MuGnx9w4vzGGO1a4CEtBE43zo33hTD7lS5qOmhblFjlSy/ggSsvX5+juwiUutC3fTzT\nOlMpgTgyQUHKzwUzCgWuqIBIkiWuoEQmVfIs8EYbKLAmpRw/8lEnd1PWQGV9tykXJv5eTDRLPpWjyRsl8VbganssoOWmg2d3\nLNtacOlcmjM66WyF2VayyvGqnp4tx1vbQCPXRotwGI2GVDTD6ptshdWnL2vEmpuv0ULgUIzgmZjEaxWjjte9y6uK8VY5IHjF\nxxdfG4sZG0bOhpV1w3w31ITLEGbmWJxVohKUya3WwKddTSDKVxSn9tVzs4V1iZOrs1LrmkZ3mro7kHVDd5qqO02Fhquz27vT\ntLvTVN0Rrzbl6agbzarrZsn8G9pWV22TITiJoyWMUs4dA/6Ohs9zV0Mv1O3uAPr5S4gAGpiGl92a8ZwhME4c5q/IUG7A76jV\nzYYYHnzBrOeN9Lh7Ug/hZ3AiRyo9dnlKj6cMT8QghHEZfTV/w6uG7jkSkJ12BWlXjgCFvFE1KEjZCEqmmaCAhi+WVfOvmTCA\npZsih//SKQPH+TByXsmla1YlJ+m2Ck2ub3NlpVx2RS/o3qRmaZZRNn6hUnOsCbUwKPz0yXWKiFNdMYpwrjkHWNkfniQ6YfXB\nSFFzEYRRNRhKqYKiEyQQvqtXQuFJVWCMFAvOUfI+SJNqaECm40klNCNFQouDc/LBWADUtGi8+JSLOsnLlIKILCcVgG3rSZpk\nWVWuq6bKdbnmM/7mr2L1dUF6RVbBQEj8sPj97K80r91SyoE1Gc9RTDt+UVXeP8vkUDXsCCiqLWXaDbSQ0sJdRNA17KdRUKwH\nGtCpyXFsWlW+es6LHCUmzSt0T49VPTA+HKntIu89t+TBumPfgPJZ21EGWHirJklWg2NeCJw+c2HFTEP+EsibUj3QOs+bv1Z+\nGpCW1roqtkC/m4FBw+HjSq10IMthPdRTdbCxGXq+EFppzhCqg3ycpDL2xBkT2irtfYBg6bKSA6jRnsunzGAFahqdZTTBMDZv\nQ5/O4EaQWlPz66HJlSBppg6I6HUAIxmZywjKoAwTQUKtuCv5dbWepcmCCxIHnM0vVHucq40QwuQWCqvIE1HIOz6xymEBue/y\nAnLzDaiuJ6vZLEhNPbgKTuUcWfHc0Z08R/Y7V5QMjaI85jTO2g84CU1Q4Bn2eo5/SNZRoMnAJb8Kl3jdWT1s+vW0YbChGXz6\nsPMr7kTe+lSSZJFmU2Mzcl0/Fpa4wzxIfZjxEwB1FQbRVEBixseVPJX9FNinMmWS2mcJi9jEufkpsM5ngh1S5zPJ93jHLpof\ng3/q94Q0IeTbDuUWx6tCHevqaDH+K8+qSI7sCvU9ArYCNspL4adz4iXw0wXEgJ/eiZfBT//Ei+BncOKF8DNE6/Or490Tb8LH\nDMhmnIf5VWHQsTml3sjNVxwvLFoi2wbkpIITxIaGKMmGclgQImGsDoY6GOpiqIuhHoZ6GOpjqI8hbDpxjdj6kLhE7EAIf8Sq\nvMxTf5I/8bOwuMbJhd53sDrEpD1NotWC84kMLTXckuyigtPm5I6jjqg6R88clw0cMQ5tzseix7sipdOotyM+NNRvTGifOLcf\nJWQ8tWASZPK5a71Mg3uX4YcJZ12VoKmA7hFD5TL1kRVwwOeTnfBZjfj0Tfi8r/gEz/hMzjkKLPhcH/JJ5WcGtvTwzMCmHp0Z\njui4wJ55eFxgZx4dF17CDxQ/gB8o/gp+dpVXhQyRzq9fNpL6USOqv2QZ4plfX0LEM4g4gIgBRkwh4gwiXkEEYOcEiqygyIyK\n9DBiCRHPIAKLDDFiChFnEIFFAI3nUGQBRQ6pSB8jlhDxDCKwyC5GTCHiDCJebTroVS4ncwXRkQ9PUfQ7EL+u+O2J36H47Yjf\nvvjdPdFHxgDoI3DlfpzXDAtS1hQC+uIcwJzjqMPxDscZDW92T8h7RQ+nN8BRj+CHpjfAGVnBj56DvO7XV828ntQnzbCe1VfA\nVST1qJFCeNJMITVah/HHIP2nGgJ4tqr7TagfUA1qbq7qGWDbpJ7BZhQBwuX1GbRpDi1aION2aDJuarm2WeGfFDK49NydQyUi\nhwg3q+PuDGhUS6GHq3roUAQgSS2BHRC6zCO6hDUM2+/VVvUcUiKe0qeyGbQ55xGApAsKAOrVQujDRKYAYtV8KAtDixGcf0C9\nMbSvVRM6tXu3IhYMNyc+yE5wGtzFTQXjO4JKC1I0kPF9QZ9zTouHJxKxgJl4SQwdp4ZFqlMilS059y2j1boHz2PFDN2xPGjo\niagGqiOBajqNNnWThpvobqA6QANMnaURpu7R0OZqg4GGv/14hM1CZTp7n9YysopHyhxAQsUjZU6J/MMkshBQEiauFtX9xqSe\nOA2/ETDAggkDTMBADebZB7IESUkjt3bgDG/IygyeSfDfJXAE/0BHIp7TYvGKM1NR8rVw/1RrBrKsUkm8V9VHIjfCMBpQlaR3\na3X7vW+xIcg0IiOCnCIfiLGdTqn2IFmdKB7jcJpQj1RPU1CephwmCACGArAN+k31HHA+jmcvFtOnh3uwTrZhkRGJbePJ8jhF\ncSEyQlwWJC+dOSQU7k9pRP6UrErRpkpwHDbyE7P7FacQ6+xaXn+5YPDEySTkqy1vSCYPw5LNw7Bk9DAsWT0MS2YPw5Ldw7Bk\n+IJ7XYg7LT0SVoOV+YJ3XPPmJ8PF0DVZKdWukWLtICpA577NJgnyoz2JRx5/uJIDv1eW4M9NW4lKFrdoUG6jIRZypWZZkP9u\nYby9A/TtiR9/9LNtbV6OW4PS9oTOomTyYVupUZ5NLMM4v5JQO8oaQ8qnT7WzyXGMlhEY5k4AzoWfkrKDVPqciBGD0UUsb+12\nOr1Bx2Utdzjsd3cB0Vvtbtcd9SBqNBjstvs8zh22dztDCAw73dGwB4luuw+9dNjjAki3BSBHvbbLmiLUAwjNVrvXafcHI+by\n0NDlse5o0B0OMDTcHXQHLqaPoCqYGLbIvJvjn+ITVCxEuh2k4/cZW6bhwk/DIBv/CJxF8jqA+CCecKN1DHHGjlqz49e5CWOa\n3w4DsRC3tDevv39ylLwggUs4npYhy4w8y1GCBdDb5fF5srHNv1TUZ13QP55UVmXlOZpgLXm0sVe/3LdXX1T3hn6v2c8LpQoH\n88ZgGBzm595NEKNW/JScjFwk6YcwPodjWJK+WfpQB+bEd89iQuE4Wy5Jpg0rynFR/gc/L0j1BLC/+Fr377dxtlqiVapgKstv\nTRDAVoYQ2NY2Gubbbv3b2dtQtwd4JYZhLJejoX7ECRbvNnqucrl+ff7p0wPUZ1J2q7RE5QLFOVvGrAGfDHH5ScuaEaUWUQtp\nXdN8/VYaU7NJ9jCKRqOhiuqekRpYnnwZzGATVIB5jrbCJIZqYAoU9DY+aSkkpgJHEr0r8qMpzjjef5+NeUm5FEydsPPULPAQ\naE+719+P60Buht3RqLvb3h0rEReIHvWADA07o91Bo9Xud9xuZ4ii+J1Wz/RslhSAtrtut727j8IFrVFnDCSs369rsKzVcweD\ngdPEeLJC800o5KV/mN1wFzRb0NkDP/ffvn4hRHF3/kXW5HdCYUajlaUTR7kbQeWap7R5iI3o0fZqW/sUhLykXcR9DZq6KKWC\nDpzOuDbnN6EpWg1f5g7lsG/CFskmAIdDvxjBZQ9QyYsCCpshiZ7bydx1DSVXnT2rGc8X/nmAHd5HB3q5+uSMlzMOW9PUv6Bo\nwYvJWmVdaMvqm1C98PPUR512bxcGSeShz/2atRtuH/3w+tmzFoF+m4dR1tJDP+bt2hIIDdQhT7b+XJ6jScStZZDi0QE7sAUs\nQAZA0Xoky2HVyqnbDrH8zp/L4HybtQboCqycuozPt521mPfMpMdCCFtPMTVHTBS3R1yaTDPLZvTYUNjKo0oTyCdhvvCXFeWM\nVIMLNpmZvIAo+UY8yctoco+ZNyTJobjGnWo0QWl4XEqKHfeBEfQfKoVKH/jpDC1cArHAXxQTc1BUSYn32xiaEoayXGmDBwTe\nvDTE77KQO2fgpWNRzsebA6uFp7gFbDvhKb7zBlNK38+R5zdeVqHpGKWaDniHOUS0ZC9viKLkjEZoLEeKj9NYDpjQchd9v2Pp\nWNjrjLfMjZWQnctPbb1MzP1VrK8MiSkyF2EwbaE1yzURxycLNNFF5PH7WUHaihtUoz0mzN4kq5Q8YDJhzmUazKAlyiAlXaJu\nh9NtqZL8ZNForMXrw2oVTj2UheWfODJSMEM0zWuTeFUcBNNMKESTSzp0RCkeAkROgJonP775+aV1dayIqVpXgda75bZs8LmJ\nhgk2etkorUmiyXkpj1pAN/g5VglslUbj7e01E8oihPdCR4SPHWlbYJRtKAcNi2Xo7tRYJCzRGhn+w4QWSgoLBArhQkCjqqs0\n2BeOWX9PajwR2wqEz46WXpYyDyNwlUNLtdXMHM3BlbuJpp1CQ/Lxd3PnvZVMxn+HTMZfSCbjDWRy/4eZscmQ+ibNzD5fkHwm\nkJGr8XhHrNDYXqGxWKHSOx9RGWN9tGJ/EazHldudmCxcociU4r6WBWlI77ZbIhHX4M3aoUV4rRfhezhIX+ZBPCX1Lns9vs9b\nB8++e/z2xdHp88PH3z+DPdmIOnz86tXzl9+z0JuiH274k3no6s77KQXcmscs8uYhm5hFHr98/ubno9c/v/qdrYC7A6RcwWKW\nqzTMRFM/Y9Ffb170OF6omsdvfTg1wVPC9+Qdlb+yhEuYwIyuUOgb6BUaWxIP1mhuIg4iTzwjXqT+8o0X6o8j9GDNy51/R1Yn\nvEwCjkWEzyP8OMySHLpxJd71WsRt5F4i+g6bZBr70Xc8lrv5xASyMSPCyWyWoXIUdCKmO2/RjTRYBr6Kd+V1VmsSINSK/OLS\nTXZsQae/x6s84ZRQPZzyBHEFwKPOUSUM8hyKoZNZjfvFx9FyTn5seT+jcPm7yraKYYv48DgKz2NSq+4xsdxWhuVQcexQp7HV\nuPZrWsJ1iRRbytH9FpwIt86CAA5Q3MrTdOsMkjUkXAMl4EDJn4T7r2EFxhKRYPHgcvZu1oU9Q8yDsA9mTFOYcY8mR6S/pvBY\njAHtMq8OXz87lAPsktwvkcTiwy5hKi1/2qF4HmtzNLJ4wZrb5hAX+iIHn7hW4VrcQCL5xi++rkxEkmni68pGGhO1ZE7xhZKt\n97r7owdq/p5ZJQpLKzegH2v1BiJgL95AhhRPZi/mQIbsRR3IkLm4A/5rLnEedVRc6IEOF5d8oMOlxR8YHxYdCESgkhwEhQiD\nNHDdTGsy+ZCKL4tEyCT+ZVEJmcS/ioQiKEx/iWIEpSgzowTOv5xqShIUYzYQlqAUZRKagP9WU5ugGFOiBoHxUSAGyAKixbos\nqFGQU6twhh2TmaRwp8FWStL3N5jInJOTO9jIilyakVwEuU/syI2gZeNea8BZjW1BrbaZGP8kVXEt3mhgOQucKK7NsVqsjIjU\n2CROqrc8v1iCY3NlMrEAx+a6ZBw3x8e3EKQTxnFbZKqmaCeMo7LIVE2wTpjE67FN5HDNi5JFinDC+EodG8uX2etzXLGI+WCr\nhcs0no0LSMgU+RjblIUpkjO2qRHTRGVcoDiMlsNYrxBWWGXjqsXIiktsXLkWWWE5jatWnbICbJnlNJeWIx2mkdK/XnF2JsaP\nERVozg8S+KrCX9WlzKWhAH/DUV3k2V6LR2ycvbcfTfV4gZuwvl7P1Nqiezb7ptygbuip4fIhrGH4eeQ6QvNE445QQXnqjwOh\njGCc7VGm0dSMmcYy18P2fnvsmmmv/LES3iyA+K+OI7zhmmLWmIJif7dXvKbeXfEeXFX04MjsAdefsABdlXtAuT6jB1dGD66s\nHlxhD65ur9jUBOJYTtZTrzyXhENZcNtZv2Yf9k2Oo0DHHWLaJLepuZx/jjkFLMz3n4Tj72bUA1U1NP+fqzQwOOL1ungI5G44\ny+dAWC97lUc9T5oPOarUrmNozCf1XOfmqErLrlepZccD1+ow5qUb9e6uK/Turm/Tu7uo0ru70Hp33CfFrbp3Vss+VxsPWycL\nfw39vPfFxGsj8bdi4sXX1uyj2M5Y1GzFdseiyv9RWoCiwdaIyfZaI/Wf0hcUWCFw4jO0B3GmVTk8yCiuEajsxdj9XBXD1nXD\nAIjhi89XPSQgGsTfVEfkXYRPs5vwefH3lRVFb+UX7+/nKzK2rpvGoDX1oH2OgiMB0SD+ptIjH7SmPWhNNWifqRIJQ2R0sK47\n+NmqkgRJwxFTqLmu3u0KjmKZCFnnCxQ+3aTy6HOVR58rNvooe5vCj9s5qWdyxHyu/OijgC7mGYk8XZXnGvJ0eJ4Bz+O2Raae\nynQBmbo801BkckWmvsz02VqfaAHxMsweow4USmz+svLxrCEE6US9nbrWWIJJcfbKelLIH13UKVFW+dANmr19QYE8V45GW3a5\n7YxrGsV2cgPBdnIDvXZyLX9vtVWK/GkRVHrL5gKbe1JW05Bkn3gRCq6t4Ifk0iMUM5vDj4ty6RGXS49QLv8Sfjoolx6hhNoU\ns7TJbJViOlfNufOw1Xa//VZFzZqXxajD5pKi6FigyzawrFW0cVmIOWwsCzGTxqIxbXYx1tmgwMEHai+XFl6kCxsPCrvOToed\nebUFD730alMeOvCoQTs99sqjhkDIDzxqwU5PTuezR2fffvvs0cv9Z9if/RpntlrD9tBtD4a7LqwW/QEzGxrI8Qw1iQ52Qsjz\naid0xmcA5kyCMUBwU7YWmNQAc4ZSnAc76J7MD3ZSZ/yyEobVJkSyzIDxEmG82kGtBoCRCZ6VJHVJzFfiGmLSkVGutmweOnX6\n26CJrtPfRm3eXEEY/zp7RWW5I5x7xIfakSev0i89ArJzJNGd4MjPa49Ayc8LTy88Mf84Zc7XVp6Geu20a0i7dqxGqLQLfDz/\n6krXqgkyraoJMs1swn9OWdtsIu5TRk6xaV075QbjLmbkvJDc139YAdxqfClXZcNLuS7+D1cml2NgpBQwzEi5+IoK6LJinVCo\nVydcfF2VdVmzkVKo2ki5+CfU3GULjJRCC4yUiy9WjYdqmtbxRnxe3FtxviE4RzoGcNYR1u7nKcpLGILtF2DEbcAXKdHfBvHv\nKNgXIq6LERdfrIL/36cBjgetGh1IeJeME1eNDia8Y/+Evrg4PuJW0MR9qx6ax0ik+8ggf4l2OX5dy69r/LqQXxdfqHmOjSWF\nDdVGUtn4cq10rv0ht22u/yE36q+ksU4DfI6XULmjh/YcL56UTvvf0ilXlNKKvPj72ufmx7X5cSH10n9YbBadgXMTWjVHc8dF\nIRdTUEBdbgqRSv4h5CnFMpgGS0gSB7FsEmZZwmU6jrjeYyC5X5l4FGS5Ej34GAYXKLJXLKGkLG+EvKCUQ8oZVTh213uhukc2\nnTjpi+ffgrPvX5jdGW9xZxV33D+LTPYtdGheQeuaLemM0BNvSNynWe2m+JYFvS68xJGIhn5QC2LePY7ZqLOARo4nYSQjXJ4u\n79SpeAZsUxRk4za6phFv70LCA0f1Pbo2CNWrZiheDEMhOBAa73Sh8ZgXqrdD/iAYmq925mjYVW4SNWnbuYTYjWvHFt/cw+o3\nd1WVLYYQVoohGOMJOYwvgZTm+JJbU+PbgCA7ElqfAgafACzNQ3SdjlsSbQD4eFDTywg9vnz6ZKykB6SboatC9TxHPvzcufLC\nwpjgU7ddyk6yYdhpFkT1YOnYS1VpNJdWtpXy5bI2Zr+lELfR+0BJIupBEE23WyOkOsRnBREKzK9CJ0Vh+V1YV+qdtyV6eb81\noK64CnRizQJ7JuxTjkyypQRlm0wED+5C8KCA4IGFzg+kp+xaGfHtnLLfjr0CAhmS94af9eYtN65QbVw/LO7cuDiPJXevEs1H\nt5YC7q8zBfd9XiHbLUC7/LmPQyexNgkc3/mJjSlSNZozjwvUBkJ+Npc7Vih2rHRdFNJ6khdls2QMkubXKLNaLdRoyy661dJE\nruz394uv0O/uwf9ZnY6z219zRdf0zbTq16l68jxVD6an6sX0FB9zpfoM8vrfRX5e1Ojn3qrD47TR5nZL0oawXJIifzyjQFfK\nP8297NjHnAsKuHhfjIEO3hhjoHsi/TCiWwNghNtok0dwyxPJKK8kjzyTDqtEIVcVmstCC1noUBa6NArNgA5cfvoUwc/806cJ\n/Cw+fVrBzyHv2tJzm4lo/NSL6vPGpL5orOqHDbReceRNH3ntfXfcdNkzyDmtT7H5zx69JD9frWev3jx/8fNLeb3z0r7SPfAM\n62ov2bR+5OwttZb9sn7g7LxkiY5JKEaqKHtJ/Qhri6BZy8a8fgbDPyFjKmcw/isIHUJo5s0gdAmhJY5PM9GNcXd0c6J6BD2b\nQM9W0LOZsxfVvZdsgn9W+GcGf9ZrOsHcNh0SXeTpVONcVkQe7dYRFfRZJFFnIlFnJVAHuoDqQmwuUWYhUeZQoox5wEpgblb1\nWSOqL5qTukKDiKJx+mbNpK6QYkLRi0ZSnzej+kx2BCYYcs0gag5AIDcJKVwWVWW5JeFLtZMb6ymJn5KLxKd+FJ3Buq1xkZWi\n8ahTbltYMwOnV3eBuC6C4HIV1xrE9V0gLko6v1w0Q4O4uA3ERvGLu+nJRqDyXvSeL/Knl7IeWYuso/pV/tR4lj813uVPjYf5\n0wv1Yrq5hcIwy7NVxDdkFEYyjD5Bu5DzgGaRY7drevo8TVLYpOVKniToA1MuasD3pBbik8QKAikGZhDIMDD3Ip60gAAlHUIA\nk/aEOIXPxSm23/3+fnssOzqvI/4D2tYPVYfxa9bElEPV8Ql+AeGAFDUAGIf5oKwhH7L9+7svhd+sgN8owX//7vcC/GYF/MYX\nt//97+++EP792v/7+3f3GJ/7wi+3/937+4zPfefXar8UnanSZtLke7zVsjDfIQk49HCaBtMtP95axR/i5CLeIlRHARtfywOZ\nqnx3LSz1QF14F9npmMYUQ/vUwFd3PTXXt/qiFa6+LrRRG8lK39Wk8jN52WwdSj/g03jKTSZl3CKSz00pJdxSUuSRyboJt6C0\n4gaXZh4ZrkPrbo2kMcMNff5IkZSF1+obu/S84SpLCV6r099ZKD6utmpGTn2hRqCWNSf6+9qr+c0UvpUybfgo+fbb8NFM1ySE\nFLg0QiNsJs2ZrozAG9VB5WZlKUz3jlFZ1pjAt6os2VxP0gyterDZZrcsyFdWvVBP1FjJejbAnwH8xICPw2DC5y3VPeEQVQ1U\nnyXUehe2vI3D3BIG4irJARmxzZ2Gq9SdH9pcIhcJUI8IeGX+yPi6dvbl1uc19f51Zexr11I2BDoaSuEQyN5WeZt6s7s2NkHM\nbuQnySt+D1/PrZrwm27z67kFCL/pOr+unocJKCu+W5jGgsU4mMIxqr/vpL1eenYSFn4dYZLrKAEiNc0qXk41dHKRVrRQp65a\n1fsonK12CsQkEw8p4hrlNrOf0maVNsxXMNfx54o/w62NcIEvqXtNV46wEb42wpswruJN7hQf5U7FE9gpPsudihewU3yYOxUP\nYKf4NHe68W3uVDylGYDEr4IlfhU4yUfe8Ub35ZDt5zRudqrytRyFptv7FcjfrlgmKCITwEkoUBRBDaNmZOVYqvJqQBUcNap3\n7Sl3mRk1TkyfY53ULFY0UFpIu5tdvcAdC9Mi/LnCHQvTVvhzUdx4Qzgx+vWkgTYcs3qkRi2l+KiR1YHE1ydq7DKKnzTQOmNa\nT9QIovXLEPKmEJ+p/BuJrX7tLNoQp4UuIm1BL3EwKHhXvJQCizQK8jThyykl+y4JNA8HBlpNyyvlCyujJUU3Fg8Vyl0glT29\nYAax1qcV+tJnjyYNeuLBFjU2m8iSR4W24wgpkKEeZT2uzKBwEZ706wldDzz0qm8jFpAn3yvUsaj7jVwuOVXfoh6qWN2VRT1V\nsbpLi3qmYq9L9F+aRcYGTIzrkAjPP8Z1yIQleBBSDB/3tAVbMxon1Ta1KabYBx84XLke53rMVOylikUsnSlCNDdwdKbI0fw+\nmFhYYsWXcDGrYmvBZP3WKk25Fp5JbdFQbX4RIwIUBxTb5qvndbtktimlbLOxrlhh2OvCumGGU34JE525TkvRwkT58f1Uvb6f\n0vP7qXp/P6UH+FP1An9KT/Cn6g3+9I5H+FPzFf7UfoY/td/hT62H+M1Tdo8X+lP7if7UfqM/tR/pT+/9Sn9aeqY/Lb3Tn5Ye\n6k+LL/W3dY2UPe1aZX+dtSpX4huKAKXQd0VNN3e+359aD/in1gv+qfWEf6re8H/bdInt3PxWoY3UvV0bSd9RqX5aVrcE8b92\n7tJo+hJ1oX9QS+gfVAT6n6vy85/V7vkytZ4vVN75QoWdr6am8xX1cr5QE+cLtW++ms7N31Gy+TuKNars7b2o272o272o63lE\n5RxxK22DoRRDLeViYl/loQMcylO4ersfDLvQ53hBU0pCG3WDMq4blHF3aOiCQF7jXXkZ1wnKuN4Q+hqQacC+cV2gjOsLZaRT\nZIzSbXbXC823ry4+X/8pMB09uDu1jCsgZVwBKRMKSBkqIDmFztdU73u6F5i1c+LUfaX8oMZB9HUkcnV1LryPkyMyEPW2Rbae\nyrYuzvF9e3hJh8YrdC0AMRG9oUyACa359RQOISFy9vCV1HM416XI1cMXd3GTFzudNyI4Gvr0RreSfQwhctVI6Hw5k11KIXIG\nB7AVgBH+WYBJQFGTW6a0pyyf/Jak0fQ5WccLnGIWAUhd+TrrVXw/4MWSm2owGmHYPzgIU174qy2cXmEBmAvHQJjSwjHRpHyH\n91l+FFvXO5pefq523X9GQ+c/pIXzP1LT5n+Y/sz/Ps2Yf1b/5Z9Xc/lPKbP8fZWVz9RR+TuKKV9DFeWrKp/8r9E1+U8olVR7\n+KRIWbO8erciC5fn/O6cX53ThTHE4MX5Fd2bXxd9+9K9dqId+vnNUF6XX3v85tu3GJmfY3kmM1+cNdbuVd1863cpfc8t3h7x\nUWDHvvGVF5OFGQ4d3YZXkR+be/2fiSHI3Co3VUjJwlnwzwTvB2ZRBdvEkzdV36kbb3+O8/9TB6OK00PULXgbbaT19Gt5HC3S\nElop8jX7zXIepCFMywYHVSr9Kex7+CTbSv1puIK101rOQ5T1nge5XwYns/MFbj3LcmeZ9aC0fgzpD3u3JF+bdXUJl9aLIh6y\n+qdXURhPb+uPkaPcI+oLk15aN2SvJFl10w2oPNYoElU3XYaWPSW+SrKweCrYyPrneEBUd5l4DpTV5HjaqwAvvDMV8NHKIpw5\nBkBU1PYhcbU6o2tkTG/L2HGqmbpLLy8NVFrRegWosBcZbwqGg7x6z1lX+qm8b/muU5AALO9Op8Z9zalxWXN6beMiahVVlU8N\n/T1d+uxv6j1+FU3Hr6Db+E9pM/4TyosCpj6W69e6mpW72UJ3APLRTsaqZ7iO9XrnNoN6vVPE9rBEBBDzy3RDPQb8DR1K6djr\nT+7Y6zd2wZ1LxZmwDxcWNQooGxzJ20z8jy+UPLKJMeqPo9QNniSG3184cHnKDi0sbfk8U7gBCGP1AC7zCmajICQoPR2KXB+C\nZ4slSugos/r87UjyAnv5w3Avb3hd4dDtcglT9OTqVRLGec2PW9bacKypkVWWUPYelZOApKi7cUvVVcuhshFUKvv8jldWjku2\nspanZJL1cTyVen2a6fVjOR9Fng1WwN6GuSRuLyxOKV6KTafFHZpri3HBareAHbq3zOqOLvOVrF7rtmOw1HAMGt4NRZtKEIQo\nEQavdPDaw6WiIMpMELzSQTjXylzrMKusAMs+1HUJpUqCo6OvjOhrHX1NT3U0y8Wuq9r2A+MkMQ5axoOThKN6UYUMWAXHn3tW\nYLznSLiKdjjrAuqWR5vfFOqB5Vd60uk1L6yPU8XS/OlKl+aPeXbpDbeWUFq/4TULQFQZG1QBy4MWtxxP98HiXeKByyDJOMKd\nB8kiyNMrEmzURqfF0uQKxLmmIdtLwTVuq0MQymBnhv44PqY/F440podBhtq4D9qO7ZkkE1RMOiYhr+6Ql+Dt0x78K7pw0lyq\nz/zYGW8gbaj4A+m33YezSkLpkO8sYHfO8DoqjM9hf9FGFGtWvCd1O5EEL5ZQ7xOdCNTjXB58zUIoABtWQglvhxLaUDD+zt6t\n6IXlPJMOI1Mygh9G0zSIFSnPYAJ87Rome+jvZUVSLjApPc5OmP2agpBzP4yzwpJ5gFc0FuVAY8maqJDpYYuEoCliTV3w26Ql\n9K3T6fmcV4zDVLFaLh9yu/ycX+WETJO+S6EnQHXLnFcy55WR88rIeS1zXsuc10ZOonev/NRfBHlQuOUmIqRvrahdzk5Nt8eM\nZ/oei5plZrTimb7ZolaZGa14FB3O8Yloktvj9aAWVBB5MXCF6SpTfTFshWkrbgMynz19ukF0Wi9RcLqhF0glzLfjisdVXXHN\nId0Y4ATxg3RdBoya1CWTMECoZHnFPSR0GeUrPf1d18NC/E4hHocGJdqrU6pK8AlW0VdUYUPnu9L5rlhYkYCjjFVuSKosw5FF\nRV8XK73W+a4tANd1Y8rGlWUwqbIMIR7LH6L0KZEfP85RF8T8NubmKA19/sCvbbOrXVw7VhaazJKveJU67DSr3NUx6afQTAL2\ngmLfF2LPKPbHQuyEYn+Jzdj3IYCEkbSy/hgCRIcdWpE/hQDQ4V4bvWM4tfyCY/tLTO6ymwDgGqHQxyF+HOIH5eF5Lxnl4Xkv\n2aH4OMSPJoGhTDz9ilEmnn7FKFNb6qs++Cap5dBsaCQ0CUbL+fQJMPbYlb6w5e8JK2d19h+449rHwrX1LzFUiGez4494AvyI\nJz/4c33CygDW5kK2SKLkz/l7ZZndM64zK/kxCzCQhpZ1cYrEWO6k1RTG5BENDlsoqBi8q/KRIkmKJ3MQ64lVy8ulOvCkLNBo\nXclD0mupzfITX6k5ft02ufsUTiXrlRSeqORQi9B5jeo8URQxqR4V/BzXXsTH7RMuyKtJoUGjjMVeYEgcBmXdu8sScakq29lQ\nlujcHfV27y67sd6eWZYI+f3727+77MZ6BxvK3qe/w7vLVtdrXpqKg/8LgTtCZiQS/hMqziObTzPlC0QsIqLVgUuyUVY8Ln3B\nsL6IvWN+d3TvvydACsR903lGgTADiiSi3svAjzLwi8wdyJhDGXiVisBpJgIfZeCZyKN9EwPlI4fJXF3dZq6V3/ousNce8tde\n17l5Fho3QjHL5DEM/TRe1tVzCWS7dBopbOZm1BVGXVtRsNtG4hHuGVBnVKeR4RVaWeJhZR0ZiUJThSI2YStHyy3wb+dRorZe\nIYT7oC3m5teFHN0Xcpx+Ejd94n4vjqru92DbaGpzIZw0qxs84cFOXOJJalt9j2f5sXLsEqzqPqsk58Eh7OX6hBfKi6fxr4tW\n8TrM2BPE1p567cI5Kiieo1Itj4ImwCp4VzhU2fdwshf6MjdVGi3la6Tb3HlxOOpt6ZabHp7jYXvjXZOoRl+mWFUIJcJNp0FY\n/uWOG2AdcYwSrLsRvoUJMGFawJpm+apzhvUExfM15CDpM4Ho8Z0Nr+ebTldbwqKaWT0nrBVnkuKDqtk93nWzXnvESkxWBZpX\nvhnvFbkxFj7aMBNow4pusMxm2EpXxQs6ozS/hrV6wHKTR6s4xmtWpGZxaCxAdUq934nToQkcWLXChZrZFhS5uI0HEgNWuVWa\nHKA6aUI/Dn3+xPlzjLLKSlFm0wYqq6i4AVR4vukYdCf548R070XassXXjQFShtNepEVZj00IoLHKEEbAd9daaK445IH3Cn20\nZP9fpCzdCa0WN7zU0vEocrdBkS0t4oel1UhLbGzihmQwFH3k94olYqvkKdVjvOjTuPZTUjgelpGuZYg5KVSruGZ8UaTWhAY/\nJc49c+MixNySS6tgtuxum41Elkt02SuQkM980xB8wIFkoN7Ld75LySXlituSSW9k0i82q/BhsYFV4K9/uOMA0yC4hiQNz/VL\n31S+m27iE3h2e6GoQvbDX9X+ahUXX9VQAh0hD1qVZ14DrFNaHTZgpFRRknx4XDpJFut2NG2WoEsKsVDCz/LbOsilzbHVB7GW\nlU2yIMsJG9UujL3KK8iLqFvLPSmhMGNwtG0KWIPlMRl/wTiFt10XbBCeevOXzqqLm7GKUziIN/f1th7mDwWVEYNctQePawfx\n53cYUPYgvkMODLvyJjhfCEU+fjR5n1gvo5VvrLCE1Qssf7SykSmsaC/me5/ImUeu02TPcFcA9rhZwF0cukugkomXh1XjCCea\npkjBbBPMprcrpcwN7JLb9Os+58pnbM4W7BA3s9WjtoP29zy/HjUTNoffpBmhSbf6is0eeZQ6f+Q1DynwEA3y8Q5ceu7Oam9W\n9y7ZHP8svFm9Nmv49XmjU0+cxhw1T2YN/IqcxoQboZl7mVQnx60EiFYNCyBlX3jNGdlEqskie6JI8/PLPIQG79cKpWAAMio2\n92aP2vvNbKxOc/rAl7Fm5MBZswK2M8b+I9i21H7fWNYqVis132zHZzdDjovPO/E5oyPpCn9N+8wVNQNe5ttvxW4LmFzKf5mx\nOVSrmXd5okBiWKAQFn9QJIoHcSWupzIBaG8zrIcsK79mIFKnjzLJCZJvRg7WN9iyrJnimgqbPlo8bPhyYCIgR1hknEi6BLQ+\nguaP5UeC4g13vc1UkMrbHmAMwiwPO1rAULwaVNHQolTthhORGF2yx9IeGyMSes2aRXxR/FZU6DT0K4QhkRuioc2QgOhREK0u\nnavK/dozlMARBh9sObahPbYVg3Fb//YMO2fkuXXzyNVh29EV0clKmYiSNlWZHKUJELoCVb4EwlqKvIJ1WIq8hkVuNFK2cYLD\nWAu9mnhJbM5RiH6CZwXx3ihiyANVIU7kMsoBoSeAmUy4goQrB0i4LwvLGPIjVYgTuYxyeMz1P33KHqWfPtVq2aPw06cwe+m/\nrIWOQ2r7QJlq/sNURqcUnXq+Q5vGfi2REK8B4rVTn8E6E7XKGGhJUowTuYxy2JLo06fkUYrPMrWEmiLtlYdeAu2IsB0pxKW8\nEUA404dtp4BbHHFTG8WqDtU2WgCpETaqdTH9FCftqDo3P1rHnxz4UuDlzagQo35J7IeiHxPIxTdl36vY+n+B/pH5GB+N0SFp\nswhb4rnKuJsPXU7wngmYCJ9vEWbWPCw9AnKUZMoST+Ql9Yo2vCnqZITYZocbtWlXENpJNZwfRd8BAC89QQfLUWPyyK8AsvKa\nACWUoyAXzkoSaDmvqx0fhv/W2wpB3SpvK3QTK9U/N17WC5jm+VGQITw/aqhmBk1/vuQsScfANwVr2qYRaEZX0kwwd+ySLZ2b\nN7YHYdF7dZssJar1Y6d+7rRCJywwJJaUgstd9a/vk+lGmnW2miQnfHrcPoGT7PS4d+Ll8LN74oXw43ZOvBR/T4CjnB73Tzwf\nfkYnXoKRXTSPPD1Gw8LwM0DTyFPUovVm+NtDs9TT4y4apZ4eD9Ek9RRVwIGJnaIKuLe80+LcHcO12YzumxzmVb9qWF3WUpDW\n/ath27JC3QCHJ0STl2jiEkIoMI4dD9G4ZY6dDI9R+QDHL0SbmGgDE0J9DA0wNMDQEENDDOEAo9FMNJIJoRFBpkpcXguvhupx\nqSLSdKBBD7miAw1xyPUcaEBD/KtvEKo0Ke7T1XtUV1JM6N5DXYMuSvkYdvmwwCTmfCh7fHQoosNHjwapNNnBJVAQ2DX8LCyq\nnwSVShek24bPBJtTXfQCszG1Y0i/VlWrlym+dqK4DclAkEY4CsKQdAXphKOECslPVHZJGl6933ShsfudX8J7aK5kt2U0NVf8\n2zJWaa7IRYFa83JZUFgsDAoDArX1ukDHs3JlUFisDQoPeVaxNFA/UC4OCqvlwb9cnhuxtS0wtS2wtC0WhKtnTo6upcyyeZiL\n2o6mjlViWuaVxq2FXbGJZ9gOk/cCwq6YYeAto50Z97dpkAIjTSatZXvwdmAFx0o417NDD01DXMLf2R6Nd4SWInA0yYA7DdaE\nj/6icYi2IXB0581LCsLowfYe8RkBLpYiYcQPGwsK4oj69UgxN0aD0Aa2blBEDYqoQRNq0EQ2aN64RD1PbNJhPWkueJv8umiV\nT63EjYMaTi3i7VlA7kPenks4PSe3twdtZt+vPU3VnqYvhwg7nOhhSgpNwqERreIGNrBRdzXo93efOWMwPXJ4YCYal7w9kRwg\nmLLGnLcHZoeGBtsjW0PTeNuEvbfbE1F7JtSeiNozKbQHey3Hpz5rLHh7ytNFIWoKhQbYvlnjUDQHx3tW1SC04v15DWpKhKYI\nF4dpRsNktgctf4uxgVbTgNLwrER7LvEuRr3iSPIjSMvfIBq2rRhbrXhBznB+W7CAvVvom3Jb3bOS7/qzeHntMIjSV4t4yIbT\nFsRdo09g+DUvQOPQPjOEkIFiSwDU/STKZ6Cp0H0Addnw0On2GKHz4L0qoGgz12Hh6PJnDjngdIiTG+NumBL6ZxSC+cWqIeRS\n6hWE+pSKoRGlYqhDqdcQGlAqhnB+/7zTlJWMpx1sEnyWZVlVpmhW1kww1eKN/TjXH1lhS/E585hw3jDirN9EcFkrzk/OOLs4\n59zgQrBdh5zFvOQc5FLwh1PBiB1xtvMZ5yrPBMf4UnCCBx5OAXvl4fijk24cfXYFP1jtY/w9Ye88HH/2o4eDz6aYCet97uEU\nsL88HH/2u0eDz37BX4D1DfxCphhzQ8UZlUJPOBToKx8laJoHlu5BI6k/bkT1541J/RuGhnog8hVEvoPIvyAyDljGtw0/gOgf\nIfp3iM4wGteqX7+C6GkA8b9A/Iri0XHJQWMGkOcAeUGQ+xj5CiLfQeRfEEmQRxgLkGcAeQ6QFwIy+T65gmiAPAfICw6ZSMsB\n7GuPG0uAPCXIiIUA+RIgLwHylENGjDxE0JcAegmgpwI00fsriAbQSwA95aChxiMA/QxAnwHolwR6iJGvIPIdRP4FkRy0i9EA\n+hmAPgPQLwXoPsZfQTSAPgPQLxH0BgNtlRyOycCRgTZkzOh3V/zCmIuA+O2L35GM74qAzDiQCRKkK2HKnEOZIGG6AigZM0Fd\nhUUY+3Fu6J3aiwgOFojOsOoQnYE/Q2wGBo2w2cdfXGEBdz4QcOcDASHzCn7JPVOAyDzHWPLKFBAyH8JvF5dYgLi8xFgXl1hg\nYvJhvdbI6rBZN9M6etXJ6kl93gghPG+kEF6gfYv6woEZrzVyDDZzSszqfn2O1i7qC7QaXV+h3ej6ygGswIwIK6fymHEGEDEj\nQkcD0RPMOK3X0HY0RCDcWSOnqhHirInZsRkIkUt5CL9slUPI5cD1DS4NGf7hQxrw03/ORwv/8DHGPyJ+gF+DEz68+EfEd/Gr\neyImg/6KlCF+Dk/ETNBfkeLyyql2l6p3ef3idKnPr/fYSA1bqfs1InCci6f6yL4EQQ9IgUCki9RcpEnlTGXe/k40dDkadjga\ndjkW9iwsHHAsHHIk3OVIOCogocuR0O1wLMSBWopBEWgIlH5WXwJbeFkn3pEQLIGYRXMG4Sm6dKpPYSM4xPTmivIeYq6Gj7ka\nK8wFzC3mOgOqd0npM4B1SNjnQ8wCygGVRYt0kOsl8lf1qIl5o+YhIR3mmjcw1xKZ6/oS9pi8fgQI+AwQ8gzw9iUebA4222nZ\n/E/ej76CY+iBdnLVJuLIsbSGvc+aOBIZjEQK/QqpdzMIT2ExQO8cygzTW0tEtqieNTDzpBnSsCSYmdaMyAw4UMNRzJqJgJ+S\nyXYcacws1zZlBkR5RgEgw7UVVYFjnsE4prTolzSOWEWu2wNLpnZIVfiiCFaBmdEAIDY+1+2B1YKvi5BtRY1fUeNzmlmfep3r\n9sDSPKPAiKqYUalLqiKknJc0+yFVMZNV4L5V80W+hFofUh2XVEdIrU9Ubpf6mlDzeQ0hNX9GzQ+p34lsEa6tlzzUpXIw+IRr\nKdUyp1rmBGNJMJaiXI96ACNO2JhSDyLqQUS1LKkWmbtPPQCohLUp9TGiPkbUgzknlJibU5RKyx+bT/32LhmKXTIVu2QmNsdQ\nbI6p2BwzsSeGYk9M5Z6Yia0wFFthKrdCIQ9bIbt3KwGq458GLos6/mkgzteJFvENso5/GoimdfzTQBysEz3iO2cd/zQQber4\np0E4Uae/eyV5FS2VRsIbDp2OjoRQoUGiK4hx4TI3oMsxV1yQ8QvdQF2KueIF2Mgvcucib1i4PDOPae9KNpPw5sUwJ09a03ub\nb5lz1uTwQ1Hfppp+/7KacgKtb7Gptttrev+lNVX15NaaCOes041po8MyV4R3igDfpyWT0JKJ6KJsgpa+YKcD/qXUpEndb4Rs\nwp1ewG+EfBO0ZULuNCIGqxjSV+Qow6d47jJjRTl9hmxT1Agr+yCs+my4lzXmV4Y2gJkHfmq7bSwjSwgJiJcu8wlkWvUkIa4E\nbuNc8JCI5nN87nYk4W5HyAkJmpTNGhkMpN/w0UNeA2WG0E/IgiSGUG5oBqwCXoos6dZpivdNwCREkOcZ3aic0c0SHAVhig48\nPGe/gr+KqtHhvOY2a5eNqePUX/LzeG3ROOMfuIEeNp/xD7oloUN8bdGEDAf8zI7F51T8gB/Ta8vGEf8Y8hK7CKXxDGkwne1r\ny+YR/3DbsvylI2JcXuYW1o2l5g3NNLjPMO9xpQh+sV2jAzGde7GLxp22FF5R+fCIgUwcdsy4JE90jt0T6hTvjAFKuFg2DjQP\n8Qom85oo8o3GvTiT3LoSZ220XMTP1ZEhgecYghEZSUL4JPqgVhZklp2kLWrCzBi3FNMpxeCGtrJi+qWYQSkGt7+ZFTMqxdCW\nN9OPLgWvclGMTy6XsPcBYno+Pot4iV6Hr4I0W+Ij7sfAftf0fo3lNEeF1YRGmLOdWo5GKVY8HDbJAHMtbwSOSJl7tbCROjyN\nUENI6KE/XQC+8Jo1v5FBBh9n69BrduCElfHPPeXhDTIHCWb2dcZCtpL5fu5kULwNt4rddMZbz+OPfhROtyZoWw0wJw+2sqss\nDxZo41+/zUe4cmEej+nOMsI1BmMvrjSjY5f/4EUI/IzwETYS95zRscgzEFnb+DYb8csK+BWZhiIVIDVdDPRPhCg/Tc7PaT5P\nzlN/OQ8nnzE7rpobV82MK0ZvzueI6BvNT31Fc3PILo25OfRoaojywbTMitMhqWLTlWn3mAOrN585CZ26PQ1tMQ3NuTUPnbqY\nibaYieaiciouxVQ0q+eiLabCLUpL3ONtVyqHoaJY+tAd7KWNBkqa5sfpCUr5wI/WbVcKdiUjahJMCGBCBBMqCx1y4Yd4mg8b\n+YllnaNsSc2SYCsd5tG+Gt2YCutqIb+oINtqIb+cIIc0Ib94yBvy2R3D8uEdw/LpHcPy8R3D8vkdw/IBnupST/D0pR7h6Uu9\ni9OXehmnL/U2Tl/qMT6Q6gm/SB2EiOspvMnZbwtDraDtsHfy22VogZPFShNUaiv8KRQR2OuJBPK0YMzsNLnFPfpp0jp49t3j\nty+OTn9+ffDstVJ/pBfR+zpK5y886f8uJ9HUqSIYiiRQPFmB40NwP6fRnlHksxxIi3H+x51I85ZtdiR9anqSPjVdSdsgtO/n\nL3I0y3JzpABjta/p1Hxoyfirhs9fNRL+qBHxx4wJf8xY8ceMGX/DmPM3jAV/w5DOpPOyM2lhKtHHQ847lEHmlnC1l9LEedga\n8f+0y0XDj1tzxRbar5WZgNJzhs9RI2nOJrpI29noh1o3rQltW5Xatiq37cqsJ9nQNDiK6ZZZJZozlm1qmuXC2hq1eall8zta\nBvVsHDWrbe3q9qOUZLX366vCqM1KbZvdMaPzjaNmzWd7c/M3OM6+tkctKrUsuhvXJtqrWnHaKv3hFrDB2eBz+7owan6pbf6d\no1bdtGTjKjBXzpXGtdvcddOepT11F8jJ3S67c2ddJvZAhsPPceBtSgVYtw+vJ61bJQgsgxeFtr+eEChZh7hJK1LIagG0gPxz\nXDPqndq1RM6nBWdIdMy0WmK08ekE38y/hvdDKRJibqoz07BpyQNi2/SAaPk/7PCHJkNMtmZtQZDm/KcdIvINK/gf6G+Qj/W6\nyPdxeTOp8zqzOEZlbTX74LmcjzGjau7Dh8GntvPo0aP2Ooj9syiwMnzyeAaR9jiKLIhNF4b//LxQ6F+y0DTMShC/9f5fWalM\nL0Jtr/PAVCit6cIB/aByQRtNTlCjppU5ZSU87xrPOX8t4KAjXqUm0j5LKhhu9lZx8s8kg34gTZD8vpAs+0JmfzFR7D2x+wcT\nxf67+P12YhwHgP3/ZuHdoED5eNufToPp9pr9pGLSYJF8xDgxh0/zreAyD+JptnWVFqYzWy2Rs5UGnbgNxe4Bcvs83AIqG8bB\nqzSBjDm/fGLb4XSb3cBZeBWM/1o0Gmtpy3EVTr1cGhZoxf4i8La3+Qe2zduWFYjIpZ8C20aqHUJLXVh+pNVHIJfe01wh6NtX\nLcHS7snnD1MJ+zRhoRzR1DovaRs4+HQSFigd2hddqxx+jdSGSzQvZJyqUO5cr1tyEmt8+k7l2IVS2OhG2iMd42TMwvNViog3\nhjEP4tUiUF98gIM1S8UWcL8C+Zr9pVp9vyLhmtEL2P1yp2u2SKZB9GsYXPBdaSyQgaP8msWGXzkz7Sc48iprTJgoF4kRRcZB\nK+Ifr/LkLRmHNTHi8PHR6+fvTh+/PfoZsOPg8dGzEqxbC/728+sXB7cWfxkE00yUfyD80Ef+VZDydf1B+GH7GGYhjJK2EuRn\n+Zu5P00uVKk0mAThx6AQO0tXWb5aPIU1EExVcVgDQJ1/pn1LRPlxuCA0yPTqyIL0AJgk72a9TuInwSxJBXjcK5L48QzQwIzg\nWV4TcCOLjigq8VROgOB/uKleyVOZA9eypemsJH0zz0mAWAw8j0ZcwXkgVjpVDvnMLVSXqqoY9ymT1So4dyxCqPTmWIShpMQ3\nlBbpDI6pxaLa2+KGssWzbwmCPRBFKOKoviaiIZ+LTQsP3EJSuY/FGWjpgUyVAywOk6+rLwZsTZINu+Qs2OrGC2I7edTvt+Y8\nMHK+vzXnW8qpjOBUDNiribZGUEDEQs9EVyVOk/ECKF00OeQULe+Uel1szgu7kb/flf/Azv/+rvx8EKIEGniU0OwWS1TY5aad\nEA4UFs0okk9nfYE/R8mLpMLXzH2hvjWtT5jQW1IgqyhZbYoZPFN2d8bPDK05yUSIWzjOjOzd3q6DtFXtkabUMslTPfVxAxXm\n58PsRXg+z/ehP6K5BylwiJLXccY6AWIPUpXA7kcs3sZkIADHq6g5lNqt04u1AsTtC1aPuSN9PaPya3q+oks48dj5yNVvAnCQ\n2ssfFnNodwwIRaWWnDFI8iJOifs1eeIPzIcbyVoisPFWQl9bEz/+f/Kts2CL2OQtP9vyt4jHRB/ZYZ4F0ay1zbQ9poDbgpew\n0KgXR4sH2pA6j2hxNhs3GRlFrbP52NZylc15HjieLP18Mn/2EU2dfLNwnPH9+xEnOd5ThMJGPba+kFv3Yq1adte08Kea0rSo\nhxsBSM9MeFIyrK5fl1SfyRf5zzNT9ASGr+kCXgabWf5WtozCCXAFzC0P108LR9kHwjaRzUOCVJSEEqtY4owxb6I3+l5jPYlQ\npKOwN/BcrVbLah1gep77k/m9CdjdFIuVcEsNz61g9UqUuU3wFaRTrGdhx22TiwVhLREfPDhSPbl6XtoFjER1JKTTIDdmLNNe\nAs27V1E8IhYLq1Tag4WBuePgBJe/KTe6Z6CxfElRyCRR+WFK6HwjRTisXIDPrU0Vk8a/di4h6vXXuqWZXQKOnscnnB0TjUUL\nL7T+TYmNQkPvcnGA7gxam6vU1jywXTSlpmbvPbfae+5oqorNnPjGSorAjGMA7HIB+32BNqllBcoR21eA/ecCKaOGbbmWvhXw\nnkXezArKAuZcf1nobHOxTttleupfkWEzOGPBvky+sLEBgYUc+QbkEBhexOocEdiApiD/yg+jpoFGeT710M8Kb/be16tcV6ja\n8Bj2qYxrrhXEECSjZdA9sm7XqirqrO1jpnkibX32SfL2033bqkuxwZ9xBq7dAv/Tp8CRN9JiFxQWeEr4Vdw5hOSrmaVSka1i\nQzB77Nx1uYFj8IXooLzvIF7s1TbdwODzCQwE/cJo2AMoR3y9Li/JsmkjgUaoWCpeZcIH2lHN5uox9T605F73Hf9tE8lH8OaL\nNpUbZcQbdpe95NaxSjayC+v1Ok9+fPPzS8u8gye3TDjxXC0DYFQhajuDcvH5NuyRN+u9HFe8dyPcOIVBNr5ZswUu19CP6COH\nw8sq5Qnhwj/noWzuL0XoA1DgHKrED30vhl9xMqU8ULAF4P0pXY8hWcHb0F5rwPgtOd9Qt9l5EPOHlLG6nG7xbm2v9Z4NrU7F\n9ba86GYpv9NWt9v60hvQcHsb7SDxK3AV75SuBvkYQ0bzulDZrbbuC1XWwjWizG0SeMqnbiQlWts3jSpf4QLSdUo3kA+4nnBq\n3UoWM6lb7w/BlUBieT0p5V8fcSjq1tLOBAMq7lWNO1Z6fmHy7GruxC35VIcFV0sBbGlEV18eq36XUlTXC87AKDd/v7Dit1nK\nXYKJ1YdBhqdjnufQaLIdJxBMvbuIRPKDqjfF1E7wynkVHEc1/AmemUrNNmKx0cBA8rn6zpp6Tngq06BQlqSSCxVebHUEJE9T\n/+I1vn+I1FMdwRBxYJY/BlMrhx3JBMaGUZgLB4CnOgJSfRKGFSn8A2LJ45iEyD9gZpe1xHtUuzlLLp/HwBQQEzYdJy07gsHn\nYRjzeDJ5ppEHk/xLmeSb6JaRwUEbcClO5HpN5gVVBmm0mX9xQ/06UVhEVjWtHYdQ//J74fHuqca202I0z8k90BXyGZE813M8\noRcy6TjII13sGR0SOStSjPwmzPNS6/gGd8SJu6rZim2pLYVj9JnlHsdcHHaKd8MHb1xMKA0q41NQzigsQRYq5s71SrWiV7wb\nwJixFVXAIRjVYrqJSDC95rtkwiJ14EmOI9pkTjxDlkFHRnqYHMYj19oS+xvocsBvcFBs4DzFBkjOU8fg3SRSkf3UiPSKmSSJ\nGZdLiynj43InhMChdioD4vHHME1iPEYJ9sqIMYFXpPHnqiM/hTOdyPdAbY9GVq9UttAWJSUuRg4JpHFTGwfqgzvYgPlRqO6h\nBVXNwzBxycETtZ8UKxrZOu56j4ykJoakChABYm9K/iwjTyZhEUIdaA9HochRPionwOqtADEEqzd5uNqbaFZv5kXHk5M9bDKH\nxWYOt8tiREXA0hlY9CGMY2MnOQvj6SEwV2J+xRcTCcZep7+Le7Fk20wBHapfsnN2NiR+MuxZKRYmSc5Rj1xpnKx8juZ/j08U\nqxzB+E08K6Mcy+jhZC+CsUz4bU7G/VhybtVuwnFEblLUt5fwQTZibinNrclWXGOh4cnUlI1QrU689l7ysPLqK8Hbo8JFtH37\nlZwY64FfNDsKATRXbTXCeoSuakapHDXE1vzQeaANeyZQPcI6DvBSIKaeON9ae2hoyzfHFW1tYYQ8R6BmCX7zswRql/gK7VHD\nxDeREK02+1YLUIXHJ7O8cLZw4MBksLOh0Q40hRrZiapJaBJ1YqfJ1nloPNZO4g31VtBUO4G32ZtBqwsJsvke2n+204xJW0Bf\n7ETqlHeoBB5DgQpeysI9Qygm0ZMoZl6ooG2FsF/JxJWXIKGZYmOCrZU6gsG40NSuVD3RWoinB/e2wYkXxfwhD90Ft/XlFh2z\nAvqRL2bS3YF6O1OPsiJBfsvjjngFa0lxdTuCVT+zl99+6Z5JppYvnbTj7OoLGTvdfEksHVeCUtRtoi9BdfytN0LBhgRWPKCh\nSynjuGadRwMZKh1+A+Oj6sQb2N9Vh9jA/i7LzgTmV0mOxlzkrYyen5yCZA3SR9y2s6BGQX6TEc4It+TRVd3H2C9rQfk9QtNC\n09ExXpmpN5pUyrhZMsjrtSUHZ4kH7t0qEAXt2ruH4BNmEytaekt5rvzO+SLwUsbkUpgxloHnUggyl5ljGQhFgPT2rjI4aAvR\nxFl8m0sVIcz3m7iB9aVCzJnUh5l44TrDVTrZAk7wJd22aycSqW36OQf6G5cMqqXCIPIk1l4hLG9D0ljVozbwyQVBDnfHMGfv\nOGPT9ZjRsCd+ekUqg6axarstaBMbhrvU4sAvtlgpI0+4SWdoOEvkx3PyRiE+Ah+3wec6ZSU/MAXdTaC5EzJtPTNtsmSmAzXD\nHPTcc3dmqISJyvdJfeXU5wzVLusrtCMJX3sWALe5aB6yQ7ZQQ1FwvKbmyX4kLI/WSzKcTxesD9zxS/T6jPdRL9GtswhcNl6S\nk2zXGPa3vxbtHqu6rjJ5DVSlPCBti7c4EGdr7mdbZ0EQb8FShS1mupUnW+Wsz/GcuUyE0Qunte0wxHV5P1fKUWyc0fTbM37W\niNWiFqp5RC1Ukdm+3obNGk+yUQu1dtj2hY648MR8O2P4gikUWN5GpqboZyJjL8lLdDnFx5SrqpQEU9ADpOpqmH0HJ7P8O38C\nNLWEEZPyenheXsGQiy/h58LNTeo8bGsdO3u0fNvF0pnlWqkl5LxCp8I14+N4+hyO8cLKXxlzJWSUXrGAByg1YcEPUKPWquJx\nDrvK2SoP7q4FdSOerGazIFWFDCG7sw3poWpBdXr6GQqDllH0KsVAORQtvzASrbPCQLQmWujgcRr4tQ1zzwsJYEUs4HWqRAsf\nDJ/HZEQnnC5LHhoBTatBGe4AnTLp79J7uNx0FLxZ3NLRFjzRdSHtUPSpqF7hnyZLSPJT4dKzCgR3HG2u+kL1VlpVG3ICYVLI\n/y7ayBtcIniVjRYtXW+mkIVx+DywmxyDzpDbL2xdlbNSIGcmADtpE17cx0en8nwhFuAmz2fWW6kvBWTOpPnRCfd4wZK9PLU5\nEaAVsRWVEfkocCKhdlkBAMjBC/Ebsfrg3ilwg548NCy+SRq7FxcgKoePKwkxJj4lVh/cWxZCnD2Es7ENURVHC9Cz5qo+2ePu\nsiB7RIVWRjN8L9qpRc0Vih2IBpV9L6UM6yw0U8lLLGQzQx8vCGL1Qe6LqcYFOuuym6mKX5Il56hOFkYu+ThRoUOjmcDZ7dQm\nzcPbmhmnTLk0W3qr+mFzUZ8h0CUBhaHgbWlimyTg5xN7hskXUw3yOjv4t1GD7I6qNS3X+nyia52isY5l47IxV/yy713WpwAS\nTQreMcLVHapSLvQtXx4++fk4s+LOKG5iO5VUbiB/m3k3Pp7yzlAhxe0PR53ubpf5cR7+tQou5mEOsYNer9cd9pkPIMaDfr/L\ngwsfzn3BeLe7u9sf9Jh/vUo5iJ4Lmc+C8BzLuu6oM2izszD7C2sYDIftTq/HziJ/8mHcxt8YH+L8aJHEU0rvtHtQHNvT6fPA\nxxCIbj4etfv9TrvDztLkIh677d1Or9MFUKs0urpIEijd648Gna7LJv40yAnEoDMY9Du7bDL30zwN4HxKDe72OxCVTJAQQqu6\nw91Rb9hmkyT1I2xEr9cZdvAznkXJRZByWP2RO9p1KToLow/U2j5AY5M0XGQJtAnKdd02ALryYzFUUz/9wEe3O6IPSuv2h50u\nfZ4n0TSIU2x+pz3qjESu89S/Grvw36jtDkUMbCowJgOAL74LOT7M/Q8hgOl1u50+B4O3ZkCoxyO3PRr0eI1JFH4MOLR+fzQc\njXhW6HtMUzbsDWGcRRwcyKFl7Xav3XY7FJcGUwLXb/foO6O5g5nvtnd7Li+XBT6vAJBhBKPGI3GwaSh6w26v2xvqWOotjlxv\n1DdjAzsWsP6vVRLCJPY7ox6Pk8gxGI36OHZBsFyGMU2OOxhhJRCTfbjiFY/cvsum4YIqHIwAhwZ9/h0Y38n0XMx5p93uQg/Y\nLEyDszQEnHVxgNzegAFmALbINQKYMIJBQ2WiLBdT1Rl0d3sdNltN5lnoU4vcEaDEOW6cZ0maIMIArsH6OJ8nWS5hdd0BZGWI\nGVgIPgCygSe9bmfkYhR2AmpwcSp4nd3OcLDLw1dBBLgL7e21u7ByGHVR5p4DV3s1DS7EgoUWzJNcjlt3d9hrsxDYbj/G2Xa7\nvf5uv9OjqPOERrHbhRwfk/SK+g4NbDOBfv3hLjQZDlj+R7poghi320HMkDEwstmcynW7MNyRfxHz1u8CLo+GAxYFgFGAebMZ\nIhaOLdAYFqGiAF9KsJYAxXs8Sqza/nAAzRqIOFxkLgwuYPiIR6kBlAMDdG23g82iVFpvsJg7XViYIopj8GgXFp2KKuaSg9bf\n7Q1EG+WKgEiYjo6IlEui4/Y6uyNRrURMiGh3e6IWvSSGu12gvF0rOihG50EQiWGBRsDS4vGqmzA97i5GLpCGdXbbFBT4AqiE\nUxkBKY9pSPoDIISSbCiUBWKfQJeQdg7auwzY2nC1MHYBQJpht9MRCWLp9MWnpCKdjouYLWKXq3QZBbBwgUbDnsMj1Sh1R8Nd\nwAUZrUjHbnt3OITRE/FLvHvkJQY9FzCCx2tC0QPc7LZlfk4sOE63e0N3CPWG01gjFgwALC2IjPMJHL8WuIN13N0+AAiz/AqO\nUXITw6LJZIJ2H0RMZ8Ri/6P/Z6JowmB3AHgLkYA0sAkBAsK2hylAivt9jABKTGuyC1hPX9PUPxsP273dIRAzTZKBtMGC59/U\nfKAJoy5spHJse11YADD1S2AbDFLRH/SH0FUeTcME5LQDy4lH6XEC3OmMYC4o2himXncXSE0Xopf+lQ89W/KF2x4O2TLwJ/Ml\nHJ2pr/APsgXpCunFYBfIPpNrY+C2AYeW0WqBe3SnN+hC4eRiKogs1A17BKxEgRKIZUNYyUByAxhhETsYAErA9iu6D6gEnYAJ\nuRL8QAf21D5sNWly5fP1AOtsgNtEBvxUFPBsMLuwGoZMrVEgfrCc4TueSkiDdhdK9phGxnYfooYYkc1hWdEQQC92WRYGcQzr\nBDIMhoCuwBd8RJIHpL+DVMNa38CZaESG3rTbAxHDF3sX5hSm1FjnMiYWC7k/grm0kL7fa0OtigT0BsBEwLjkSP66uFjwIwD6\nCF0aDehklcNgAg0CHAPWJU8Wfp4Q1R/Cns6MldPpA+IPmNhgAZVgK94dsIt54OfE2XWxR3oDHMLWwj+zRfJBMn+wAAxKNBjB\nzsC/JToCRrSHvTULQ+9mDixhBv9H4/aanWV2hH57S/1azCwf3WQzMm+g14/8ER7V8yaFH7o7g/24UQuasVMf1PMxxnT2A/jt\n7HSNlBp8NnNnHAundkGFU7uCT0YuLKIVlIWtuda5DJzJgL57M6/hhKdU+fAfekVBh9QL9si5rZRLETdPdP4YC2HS1PO2YyC5\nQbqtrAf/EFwWcghxU+0m701+BSfmVMg8yNjX3z8pqFHISzntJMHSDZIvEOf6KSJQF3nYDHyYfG1oxHIzNsA74aDKl0avFjx6\n5A6+BYbf2UGuX8DE6N1C7BnE6igfJVh+S9IP0DsaozdLf8JvAuQV4NrsF5z6ochFscDGXuWy0vDWqlJd1Q9vXtxRFclnv1yQ\nPzEYnXfc0jG6VcNwKAzj0gOEaI5sjGgKCeko8fLwodfq74f1mtsA/A0beTOs58z3OvWwme0JCLBifJaxoIHXdLJzMlJeSqqI\nJl3myYPr/frNEUpOt1qqIZzvbzJDvoVeDr+LEh9fiB7CUq26VKN6xluPo+Xc3yKdBmBPclTy224Eje2tizCKUIkxPI+B4Z62\nth2yQJKSo2Vv51+1Py4azh+12vG//nBO6s4fzk4ruAwmKB1GDnUz9WiUcj8YaPpM2jrzha2z9PxseyxD/vYYYGcA+4+sXvtj\nCuCzOtsQru2PxWf9j9a+iHT2vxGtSBx56QCjc9w70UaFEE+V423Ebxqt5zGMFbbTbTsc7Tfl6dwjT9fIw7W6rF79l9WX8tfX\n6Rsekiv6htGb8nTukadr5Mkti13zLBKTCaHiZBo9KfbtvzbEffEwIH0w1wD03dnpDkQ3ZGTnhHfCiuyeFHp2m8EvsYTeCkNe\nE/zcIisluIactXJChuvlj/+7dvy4+Z3fnP0xPWk43xjLRb0B4yT5ntaJndV8oFHdouMKnGY9Iy28eHmc45OZO4DWm5go09xb\n0joqTSggQpWDYpW4z+iiWEA+C28YFGm2dh5cioGhIVFu2fDKTMl4FSojEFydE2spbJNWKlFCeev82+wYZXlf4HXSUx+9ziij\nr6Eij9YOju97959a7MFn2rgU4h/n8nmn8u2KJEbUJq8yQ/BMO099A7N+lKB8aBWPcA5xrVRtO/R5rjYd+jxzNDAO5ih5Q7t2\nEdhfiQWMPjUw+tTASLHYalzRGV2h7aYuNC9ttaZc2ko2Sp8L9sdkfmAnxcfG8l56kBsviLgfE30jYWE0dwjJaR3JdZsh4wPs\nar/fHTRKmc6tTHD8KGc5M7OIRr4h3tBqam27Tf9tN+STOnXGAQQWuWGROUJAqDngkDjjs4HpuWfflThH6GGvgYfCfrEMf86k\nZ1F0rcFfqKR3UdwKeAw94kRsol5OaknDh81OWsb2HS7X2jaZqJnnNxO5/U+8FXFUs52a30icMfx2mpDuMMEYbIXjyKulzczZ\nmTVq6cNsfzC27H1upZgha4aYoWMmZJiA5rwhoccT1tGON1AWHVpzL0L9Y28CfyNvxdV0Bft6O+t67xEGyCkfXVg5fHxhzfAR\npuoEK0f4cG+cVUqSBDnkcKmeM9P2wOt8/99EsGrf3ATrrW9ukLv8Lvz/yHvz9iaSJHH4b/gUanZ+s5JdErp8imoeY8vgbl9j\nCxqah8eUpZJd07q6SjI2oO/+RuQZeZQsA73Tu2/vDlZlRkRGXpGRmZERt3GvCConfCfWd0q/Sx+3P4IyBrhkZE9xTJfmgZGY\n+BJTnlj6OB/3+1RXdyxIMCdJzFU7SSrXqzHsUyvZ6hT/DFYT5e/DEFKrRGauEqG5qqUmoLHGNB3raIm7OjWk7urUkLyrU0In\nZ2u2qnYx6tflqtqdzS5d0aoml9zRlqmktXOvylTw2rmXZS2H88JIpiuknVZIO63odnKD9NkUNL7G5riDOJ14mnc1xBWkzD9K\nK1PVSriS8OQrlXzJki/LYokUyYy07j97APEeLE5FC64ktCchmTedTOY9Csm8zUQyK0IMUGHvR8ZlLD8uMy0xfxuzMRpcZvAP\ni0LDUzJMYfvhTKQMMGVg6S9qnHPRSs1xlFNVt563um53uj4i6Cjxu9FwXr2jlBAKCLmLzwmHnoYsbMd0FdXglWSVhdpQM4QF\n8MC8Js/bUHmXIQvlgXlrPG9T5vkuVJXj1RR1wCv5dcUuVuXXpevr32qV99MPqlmYZ1TVMswx6tKeVVPDseqV4VcVhXWe7ZPT\nSzBa3iqrriue8E4lXPKE32WCfMbsikXQAkr3ulxNqVfVK/pxKS+e97hFazxtxdPK8c5R+xwUZLZ2/868iLIzuDRb2k/nkXxN\nsryfzt8f5KdTFiASL2F7gE/YwqEYZ1nSi8OhmNE34nkhygfl1nAMyyY+5BSfPPwkfywvQSI873gVZdcqhZVznnbDTkQS9rJp\nuEMT2mh1n4xH4W5i4rETFOJbSKJ70yUVbyY/7OSdJm1hdS5HEbK/F0+m1/uzUTf8NSMpnTibqtNSlvJbmky1g8gMurqbDFji\nEdrQqzM/kcNInneNtLO4H1YdKD96lAzCfydG2u/exFMYfXai4FX0S3eQ4DXzFTNay6jzJsg4kKZJ2CMUg5vt6yGRse9zHDqE\nAja02TKTNO4m+FyfgE3Gg7ur8eiEKTKKopG6H+Fkkc1jZL0eJdNM9VcyvY5RqzcHYme8O4aRHF3pamu3Z0ks+lx57Ryn3fg8\nQUMs1nwy3XYDOoXN6RGsC8S1J3HaqeYPq610xs7ZweFTZeEn1LcdgkJlsDAUGkyFolBJP1d/CuOfpeNyUeDqqlNgjD5CZ8mg\nl+culKfsjoeTZIC77nl3lk3HQ5A7V2k03I261/GvsR21w0LT2yp2QfAG5VMmfNXE+hWffmI0xSdGMYmsDSsG7nDsOwR1cvBR\nxfJhUmy7oN5dFv4bdPD5fzOzRSYY8ZQVlGUmPXuVj0y9mCajWTynrjVEgenSBYpikoy7bitMhGDWntv+8UXJ27lRrnEHwp8U\nJKVtkSpUk3/+M0E3JtrFYSptrLYFu2HycPcc0jOH1/3GXGteX+Srrm2PYw29dBDXGjJRudaYtxLbn0Zyrz+NJMefBraUGNrs\nt2w9htLVzhN4plzY1cOv2dU1SDV6ag9oKjk0oeRrLGiBgYulkkMTSm6truN4ZGKwpFDnUshdUjGdYNROJ4c2mFVPlnPmr6yZ\nF3rg5avtYZJlyY16li0+DZZkYmiCWOzIdFw9RhloChZJlQ6c1gy6Kif0I8j6TuLuDDZRsv3Ep9l6IjE0QeyWE+mUJdp4dnbo\nx7KoGZ1L07wc0i42gJ1eTkaJr4NFcmhCyRmEjhC742hqYqnk0ISysXJGlZsf5uDZBGHFlPOZJBkuBxIjK3SBraf+eSx7iqJ5\neWVSmHAB+mIujoWfdIcFlZFXvgII8xDNkgMHl5ncerFZjn67L3cdKShuWRe0w9jsZpIR2pAu7sHJWS465IUeeJdI5zrp/oFN\nzFzH5NIzwcLFVNxSdL+YiVafmJmhD8E7CnyceIukufllU6hwIQkvN9EoycZT0FIs6abTQwvOQZSOhPMIyPwwB88hqFvDSLPa\nwMgLPeDe+g4V7aFDcSjpDPOxp11CAD8cGpgYUgB/u6PuTSoqPu06iuTQBPJSZOaQmqL8tCjK5NAEcqSGzLFWNydZVmdM6jL2\nVGSsazH2FsiSrdLMNLkTnw0nujDxZRUnUkMDxCkSM4g4VJ/yfMSS0qMc4TwyZfIoXxSrrI7SeI0kBUK4Gi2QzegsGW0t8SBR\n82mlWtxauaEXxeGcAhDmnGQL9EUSZS4kptpauGY/zV+GU2f1Te9ddJUurkugSfb0JVmhC+wtQWqiugCSYtEnOaED6qUu1T5N\nnaRY1ElO6IAupK4mmVsMzcopj4KE+cgLOWBqrVu6TM4pWWaHfiR/f41uSFexD7uXWGJIAawZIfaVw0tQpy3tlyeGFKJklJyz\nn7AyQx+G8tHYH7AY0Q4RmhM6sBodQwTA0nuGC7BDgWaGPgx5wo6u0wzRQ1KsJiU5oQPq7SZ2dMxmh61a0JzQgfWgawatVItJ\nKzf0oviZlYqWxalMDk0oG4swmK/0TR1Nb3q/ejeFkcPPu/cS7kKScLgApPZUqHFubpiH5pYpXVtSvdDKDb04zv42+WzNM0wJ\nVV7JPmrWzvsSegBtQRHqO5oDtyCSGfowSuZ9CRAY8n28ukEx8kv6NgVBE1GO4k9zZtyxCF93iXXzIq8oxNXLM35sIi9iaFbJ\nvZRRNI2bGuPW4zztApedSFcIr2qM/JJ57wLgOwQcL3KM/JLnOgZwdhONo+56XMiS5/KHdrd5K+TCljyXRA6+uj1yYUt5l0kO\nEfOqKQerZF8/Sc1WJRgnQ+Saygaz5oy+shKOfBN6i2WBlKxLLUD5NeMKo7rnMiFK9qWXcB+QkCQTouRcipkoPM2CKTn3RQqJ\npFkwJf9dG1SqvrbGZ5t9DedFKDl3bkDivEsp6JYhCSX79k71ALnQs0BK3vs9l2V18+cDLzl3gUDg34mBj3eBNlTJvS90EH93\nMH/3oOK1mIvKLsscOE83aYFkpDpwlvs2e1Ej6fJcnx5CpObRg3nLqDiwLh+rpdzbR1WM72YyD6mUd2Ppp8YvM3NQ1BnAKP6U\n9KbXdnvQjJo4CBApoQkgJ2mUXZ87i65MDQ0YqQ5GExdDJIYUgrrMs9ZavbWkPvT01a3qG3KbW6VHKvzWUx+eaDGkPku2GYIi\nSiwTTKL6jtiEpZfHani4t8d6RHluliWefbWskJw7Z4nxKWF6+ZBwpZNcqEPZxT/XTNhDcyi46T5K3WiC93PM5PCJS08dgtmp\nPlr/HiejRcQwP/SjycYbRMyjoDFGaGKev/zE9ZdP7u4VEL3PV371x1caAD8wZzln+EmeM3zy+g+fFMn3O4bnzTFei2faLSfz\npio8bw60582Ie94c6CdOhjPVLEwNT6kR+xaeUltZjstSBPS6LI20I9Fv9ybld+qpdOhY/STWSDH749GWY+PTtE6K5S9XIY7p\nl2WeFKuflplSrH76zJVi89tnuxSb3z4zptj8zrVoij2JtoqpHHOpFNfMKSYftsVTrH/bWmCsfzvqXkw+cqyhYifJNY6K6Zdj\nJxWTD7/JVGynuAZUMf3y2FLFxqfHrio2Pj16jllRZVceW0ZXLe5WlPljxOkr9hgkAox6qsTM1vgBcVqisWBaqFGkrdXVrJS8\nzz6EUwwuJibp3HjqYdp7JTnGXrGT5Bp/xfTLsQOLyYej38fkw7YMi/Vvn+oWm9/51mKxLzXXgiz2JNqaSax/W+oIupuVv23V\nI9a//fpGbKfkKhmxJ9GvWcR2ykLPvWTpi8nHN/rLRbtbvAvA8F1C4JshH0U0ewHzZM5sxWANUV6IWfg0scibZm1z8ar9bV9Z\n1KaZ+cDdNamNs+sXUZZ0qW0ttYi185+Q8SoNRaVfAH3HSEwY1Y2bm6QPe2vkGo0AWpdiNedKgMLKu0KdJM6xqdUlP5z+1HcP\nksOa93Q4rGxt2uqmtHr0aJA1v6oodbsc5c/MZrpUVb1KUYaC9ss91pWmZ88uXd26ZGHDPkG/1hOrU2L1M69rYjeNdlbM/3r7\nK7YS3O6L6ZfVj7H6afRmLH6YPRrLX56OjY1Pfy/Hdord57H+ndv5sScxZzzETlLe0IjdND1MUJRxeStt7m+lF+lOxn6MpsLO\n/mrk+rpAlf2LEzYiLpWm1+n4E9Nc8V62TQP1Wi8RtgsRIhWy6/Fs0GMxhxlOjy/FlSellozRZCKqIW7Y4TNi8pEPLIBDvnsO\ndKipUO+Zn8sHvE+n21V6ac0C9yRSROM6ciQUpQse1o1bxnzhD8S20SccUN4u1+YBCXknoja9l688JjN2bz1ILPvl+Xj0ejIY\nR73daDDACDBoILy81EZbZ1IgcQbreRXstL6BiYa3oGKCigBpvUI06iknCjzAbw/3TmltfatSeJ2x8NCvTXwYIHGErhbc5sIq\nvcbGdCKUshaO1VM1SlK/a6JtyrdmX7JplE63Y9H603mJhyYm+FnRh807HRre95TZ2T/xIcWWKv7b2IiJtJI14mL10xh6ItqZ\nPdRi8kFHXcz/muMnlr/0Y2gSv30lNBgJEkhQX0qrRXv/zIRspc+yVqpCi2Ol3serKai74iOBDyuMNZQsZrzpyZi1EnNiU/I/\n8VLxcmR7hWG9RMKey0dfrLla02cJC3reybx+mKUfF8i2ylFr4Nt3xSnm3+I/d3YAI81E434mbqcLmYDsfCZ+By4A4Bb/ucN/\nPpu+AYwI1LERCP4HMNP8BmakYSQPJvrjOLLoPoAtdrwAbAyNiMQ/gC8v4QcwJpw22Q/9yGRQzoau0AeE8FfDRRzfptKpt2KM\nzdWpMfeIvMBTpHA3LSZEWsFOIeGOJkgh9vtTi8QfU5vEQm7E8jhnbwZjXoHpggosYH+K7E+tspH9t+4zRQvxj6mDmM+BUAPm\n7FXjEiyv1r6F6Xc/lGn2rFOx/ftSbNe/he3ffyzbdcr2b0ux3fgWtn/7sWw3FNt80TCnjbO4LlNecN/UWq3KQlVKTU0vIXns\nqAY/ipMgxaT025hTKdDZKWX3N49n+b+QY9Bk/kCXPj+oEioFBgMnqhR0W9GxFXepxC7tXIcVZaqOxsm+emLNN2ew5ZBg21Zr\n4qkPWW9ISVyVZcnbfE+FC2GRNqDuhm2rW+YtWzeWr8pi/6syprACzFGXwXA9VueUglgeM/2pj5l8e0t51ISN9zoZTWvrUtfk\nPhIFlXcPo9Koe6l8TpamwvxsOWRQuv3CHqWzjvoX30WfT4OMB3HaBTAZ8ukfPBMUj9ep/HUptt1ys50s/aidazQypu0Dnrb/\n8qCn7WYxT2QI5l58S8/QpF6VqVe6w3E6ud7JSeexSLOzGENN3OiDMQyBOtE7Zxo9ljw4NyPZ6gwVQjkUO0S5P689rc6dx8Q8\nLEYPDX9Mz52YhgKNZ+qZbx92PKdNEX8qfu5j4rv+9p/9EnPfuE0AYh24RftisBRG1Vjv4w/IwAKvDQasXLr4XaaPvrjldFE5\n4nWULceVOkHB7fpL7CxxHlSVTkhYB/q36SpmPWvY7URu2xkdtWFXQwBbYE/2KDkPUL1cYfTlmY9OFjHGnU2V4V5EV0tFNmwZ\nz/k8O6gKOZfBi/GWGTNEU+RilD25duLT8tO1X2GV0IFv1HaolXh2SVkJ3zcYRRuPrWnJU2wB2P2IHjRsSNOcnU5q1ys/orM8\nOp1AYS90fnFx9GkfFgcp0mOCf6nYkGQM/mtaGUZ/xPLNE7qbMeCE/KFd9S+522LWSvHbfGpvlyLwLp/Au6UI/J5P4Pd7CLAe\nG0RcBBCNVJDpiGwV1GchMWYa5Cd0TrIWkRiMx3/s0GA/WVRRabDmifM2MXJ9pACGBvOc83jjju+8+4YYRgfH3TTHTtISDOIr\nbCdqq8saLklh6w7/3OE/n32xyqhwIFF6QaoFeOMp74mfpTQeZRbGLAylCMELZWRQRFb5/PVr1bwdNiT5EyltngQoBz4noPg2\nSsqpoFvtL85KqKxl3TWS6RRSLsW5ki4QQtBao7UklG4uk+zloXVyoj05xOx0/7/p+bJUE7wduF1wiBXS+M8ZyKKsEBWG0WgW\nDQqyPvDjtlLYGeBUZ0rC4K6AZ+NPhnF2bYYxfYJBtJ70o0EWP6n8d6D9LBqjBU9ieBTQMugDgfqnFPBU/Bb/K8kDnrnpYkMd\nX1p0932OltCZnjmUprlDaYpD6R/TPFrKi4JHd3pevJw6Mdkof8NkBJon/vE0Snw7iUa9F3c8ENYlTPP7qEW3jFp0uwy10nbx\nHqAlORNFSse0Hrb+iNvDCai7pVYxyY6jY29DVG5LX78uyL5bnP25VNIeqR8+/Hd5aq8AtJ5CdQrX0U1cgNK4a5WsUuhcxwUt\nIQpq6uL9zCD5A+cADHUbTQz5ec4S+8WnNfuFiFSp8f5v8DeUI7JKDxQlGUP7YdKEc6EFChMZVGJY0eQsPL7iYdvkz3hDeqAr\npkhLj+xZ1Mq09Bgzu6TW6zSP1vgB0oNPR9if8lkJn/dJCD4zGQbKBB8GSAEnObcEQag0hxyywHPXrSlstM1WEeq+apTLaU7I\nzgxPkJQfypQ/uGXPrzrj8z9nURr3GK/MWuxBTR8Mwtz2Vdx2gdAsHAtuu89mrW4+t+OgWwrYe5g0pzZd2Q2o+ixZs7lvMOJr\nwlkW6sDMKew2XAloQH+bENRzV8lBTg6FGxT4vdJPir8O3wJl5PBM78MDZ/vHZJQQh1+/TpXsop6fxIbOSJrdeJxbxa7Bg2yO\n7ULFYbDQjxKQOJXCET6TBEElZFhP1z0rFDnjBSV0C5ybwjgtzG5KT5TkUe6+xOkiKhwSSSSBpiHqIhJwbM9uxMdYhfJ+2uDG\nF8bBwBOxuXxSEubbHu1WgjCd6mrkHpw1V0AcNUskGqZwHukrR/DVxQOhGVXNd0AO7Dwbt3ZgDnXf73wQB2wz9bMlXTfzjGvx\ndyj+HgkTl+BW/pjIHz1pBiMIKcPydnEneBv8UvrSr2gHn2mws9IoBddG0ltMGhpJv2DSEUmKALFeCm6NpLeYNDGSfsGkaww+\nWewjUfHjlv04Qmj+Q4aWjDG25G3ldmVSuStP4O8tXnsn2X4ySqBtezFM3mKPHzVfO2F6AcmNLzkMykjEhsW3lh1OaOhkAgcu\noesAOfISwk5k4qyHv9+S37+o3zMJ08Hfb8nvX+RvHujjMiQnSa1LafbBXwddhu+t08FEAMw/lOgAC96GEhXG2tvW6uqOlCi/\nhJfASwCN/Qs/hAoO4BcX7ZLCn2EvDt7BP6sHrT+fvWv9uRo2Su1i8v7P1eqHAP/U+J/6h5KYusdi6O2Jv6fibxTbY/GuCLyc\nkoGS8XEYxbxDTuV4eBtiy7aOefLbUnDMxsup3QenLBjr21KppO8AYCu9x2NDy7U+ioO3ZKTtMSTsltKz6vNybbvWGrzfWWl+\nCI9hO8x+4n3LMeyL+UcdPz6Lj8YHaJz5X9zgd6LBSwH/VVO/sNnlkvGGPXjgh26L140r/16+JG3M5YJArvVNFN66HIH4Y+RG\n6D6BOeXVxN5taKsElyJGZWJ7I9ke19CiwzARrXT9bNi6BmmZSJuG64C7aW3Jo0Q+1jI55sTfsfg7EH+7UtSKv0LCslW0ZBYd\nG0VDZ4h2PZIeh69XYfN9q79qIM70Vx0WNr/pRgByL8ciKLjFhzb+rAmIChrFN0KdsG8Gc84QhsdDByk79lJKsPxBThaU383J\nmiBBlFMzRBc/uvKH6pgjgLqF/8F6jMdVKh1mFM4qnExG+gRo3ML/YJ2E9LlnDEztjshrV9YhWW5mLb9tsbse2rp6KK6Cbgx1\nmEEdZkbdoMzcnLqR0zLvL9Vcdo/O5y7Ql7yNrjjIN42MYsvCKHfLwfdKVJ7G2qLoEi2KLtGi6BItiubT8fF4xG5GYvQmq0Q9\n7gbUs5Uu7CC4ToR7CXUJ3IcPYkjItZ2uces8EJJ1ZcZ3UkOoyhHZTd3C9ySUUK3bZ5PWLW50oJSMvVgZxKB396xKPh+Gg/e3\nH1bGFXw6xx4vgPwdV4R9rMidqVJ6UGIP9j49IH39/mh19QOsT0P4Myc35iAFr4NZ0C/NlaEeu0ITL3juNXJVGxCzSbcLZjbu\nKqJBGke9O9CoR+WEA0oTVvWyiLGUSOMyBqUVbGsvYT02TPVjw/T9+AMIz7g4QEE9NWU47vfm6izQc6Ky4BXjeyTLnjLKRp5B\nV/bDruzK2bN+a6b3rNfQ4rMPKJ6hlXFbzQ+whzAC7VKBaDiQyfaVbf6WVz6+pEqY5G0MvA1C+RSyNX42aI01b90wwoqwjS2/\nXeyK5b4rDGi7FeMSUR+0eywmFnndtW+1ie9dZxApD7x9tGswnfDG9zrhdc0llnvqGuc9dRUPmaSH5sy5YRR7Kg1BRk8XR8+g\nNHjf/UCvBmP4DjFRWTbG85jN6fCLHt7o1bhl3KKy2dCaagceHElMWP4kabqMZcokHU/HCM6jBlVAKx0UBWZpnnvTSmo2wJol\neiQl7wcfWoIdcoc9AImjXP6w3JK6TP0yb/Ezn59qNmHfpNRl+XKhJHPPip7or/W07D+D/+mhP4Rp2f/QmonZaLFYms/o2Eix\nGrOAPWmfZ7rZbZsLjOGjM5aewSXvFI7M0cnoisv6/EdswLqkNvYdhsrhNnaGkHUk/YWfmm6PxfGp9ukX8FMkyOE/5mhq9H1P\nqb/DvOVbLVjUvIJBaD9HaOmTHT7hEvNqXdmqJOJ16rSk1frYP126xvo0g/Wp+6Hl7i66qLBJmmqJihesT11jfcIZAItRhtRN\npbQvJ4LcmYih339//YFUwzu1QFjN5vnWRPHidSm2FyV+NqsWJXk4q8N/qbZR61JfrEt9sS717XVJjvmYDoEWGebO8BjLp8X6\nbCzOmS2DnxZe3wwUpcBvMWOn+C1oYjvFMqHSK9Q3PU+Vhx5daTF3xzfBfwyDHfEEbRDsy/1mqjao8sdY/ZB71U9y83omn7AF\nu+rXqfp1Imm+kD8+yx+HksJeZpjl/TFSZnm7VuhnqR9yRfFt3/tS1vM4VljSXUn7vVh5yuXPaaf0RdkRGc9FKayITRh90clj\nuJEZcDDqD2bo1dhwsmdPHw0V5mCLgH4li/xewjYqUXq3iLyGCoX+A+2agKoPgjOHXKlkNYllgKcySs/1b8nktk6yG1r95MPW\n18KmrZiEt+UQ7AkMXY4f6ujYoNrXwPvkffUDD4lB1LUFXSDXD38LwhphXlCluRdUKfoswEXk61cRRCSjQtXpYyaDq6UFpeOu\nIJvPmf0kPzg7FcdgfEgaqppq6jRMvDfIGaTnXR9DxRK/KPfvuFMdaHAc5teRBTX+5z/HpS+fePB1cRAmm5SHgFShbAfPuq2B\nblLYeKNqh2saKJgz4XNq7D8Y6WO4zuj5p8w5Cx9Hway07c1gJ7QwiWd4ZceOhz5lZKMDY78bZdPFjW1MHPnij9td/TZOB72W\n5V0yxzogyTXb2xGh5m1UlmMafJVAssun5ajOV/CZKVaggsFRS8FPRUZsNI2SUcYvgQFhnCZXyUheNWFKIh1nCCZ2MlgeSurq\nTuF4rj8B7mcorx9hyD1W6spKHY05isddzlpWAvro+KbosGvU5riLDCde60jKY4Y2HwYYr0pJ6GwXomGpNxAeLfAuAZ15np+f\n8NPltGUcGKi+j6y+H4fyCHsQ+o04umb67AaUISulhuPdPRiD/YzUeEGjIxoEi6cqXa4478RhUyDn2hHMtdvwWs61o2e3rSM9\n1ybh9fujD0EPNLCJqWB9CDr6ynsitJgh/1sK2jruq7hx1zHBBfDqRGQIpNUh/y5pOXAZdoLjsN26fHbcuiTn13ugqrEz6stS\ncKp+s9PRWH/i8XX4mu/wg14QB0nQxVOtYC84BUDYtjCT3H7UjVmNOMv9wRhKv3zaQLNczDOrHVrNEAjzw1RaRqkjdhJxUzXL\nrdssdu1V5SfhEbT7bWvyrNeakMp3ZA0n2MzyN1b+Un8ZdY9I3TtBO7hcWPUJVt2uFb7YHeiF8z86oAZ/xYC6hIEEIwgHEIyc\nv/PAGXz7wJlAM8JIgYEyMer4nQNkPldn5f8aFkeBeoIGhMdcVg742R3zexaGf06fD0IiqjvQKujRB68sAA9U9XFp2w/BqSpK\nwwRAg4F1Ms2283tifYT8PWtBHNGFWCos3XCKq427hBX3MnUo1302ZavX16/dn6e4mj3Hsra/SOjtbjDB9XMbS+dbwGDMNNTt\nEWkmaHirmYJB0C19GVVclW4MO69S4MsBtSInpwt7s5IKsU07BYpLIRP2WUxXnsEGBnr6LMtR56DxdvPyBiB88/K6eM80uwn7\njDu2hE7Gwkz+MNNsnGVAH8gEfGcIG44sn51sATvZAnYyyU7twfwwtHrIkGF1Ad5O/JeLONJhO5uTB7x9zstjvAnzpvvYO+lC\nGUCKG0xrRGYGgFZn4lFJCY8IVaZlaVCuqQOOfvgl2h4Hl9sw+sRTxG1hp2O8FKrCdmekX8sUFUN9UQaygsIi7Es1eSbe92U6\nSOxVYm3ca7Bnr8Eergb6cg10ZBC/Yc3avos3cONb6wGcPlwPvzB3PdtxcB2jv6PtacCc9W0nAcs4j69YvOLtVACohIwDqu9o\nbmxeWikVeym+diXfzPMk+Y70qRG7ERIWU6C3vf/Q0qdu1dZR8cnnJ8ETqMiT2ydBuYb/n8DsjAMUf7DzMwFUfpkD1BgAZEig\nGvxfzCBSAKi72WWeX+YADQVwx4EEgBRGTTu/rADKHGJNW0HwU8+B1yzCeWfRVe8scgwoBNwsD252o2D6UFHic/SoeBtMYK1m\nq1dwLFbq4E6ufTvh5dPT4G14/BRSf4GPOpqyHMOfg3AP/v0zPIV18V0Yxas11ln/gs76h3pUOlL2P3LTH6NzwvjZO/iH7Ezj\nMItX3pZ7xOoUAbvxsz/hHw34a9iNV3bKv7RG8fvbD+GvKx2o1/vJh3AWr7TxZ+9DeBB0+Ro7wiDeIwzfDf98xscSiFMVGFUB\nvvdz9Xltu4y39V6sPk/uxk9P1UetnMVPUaH512pYm8/tykUxr5261sT0WfzsFP4hV4ZxeL0KCX+uZHHwq/xdBEwYqQPMLM7w\nt048NxKzWN6AdgEfMtE2g33/Cujs+x+r4fp8rM9+h8E/oGuD4Wr4j+B6NfzXfAn/akReeI6/dK58noSvw7oFFNkyJCm5ychg\nI1jhTsLiChcqgXARGogMKVQUAEkwpE6J6AXttDgi96fzlhVSFjJZ8HEjJ2E5kKyPu/ALH7RwLU444f/6NeWRuNE7Dv1o8g9+\n/FGnHw36IcBEYA/+oV8Clp7jN4+8y897BODzomko8HqU4API7PU0GWTbBQGVYZzZlGEXpvysqdCNRhiL9jIuMFWqV7hJIv5T\n0iiW0AZ3GAO8TkL7gZg3ANcKt+VXKnWybXP3kpaeaxB1fClT5nN1I6s66mTq6ShhoNKaPhvJ3c9Uz5MkxN7FbiJ9l/I70/h9\n+iFM4B9PUfEVLYpcZ3qKisXch1L0XYZL8t99JCnHM67rtN+K8kTn+agynk0nsykbPucTWN+3o2nl0zj9Ixld6URxjzC9Cr+w\nQrfbacD6ZPtkOm/dRGlhdBV+ZFvHYQQ7mFLhy+NHV4MLqa4WQow2/G+uuvAhWVgpDMe9ePAmiT+plJu42ywSK+xapVootR7P\nPwaJl/5+GnEuoQCOCxhBoar+UQTEJcPLZHn3m+ggNk5zfG+amU+kO2H0CqAvKmdixDoJL80bTO4MmpMMpY/WPtSNxS7jycmV\n5UbfcWe5hOtL7gmceJPMnHjq+eHERf1Z+6GpB1QLGEtu2HFxtg14yPIeU8zgAw+quC7M8jJWDSELDk/2MA3LxAdBMkgPpM1V\nS0ag0yrNgPvZhNGHnb39HhWiGmhfN9vvq0EVf9T4rznxnVBV2MfYOvxgw+yH4zjuSas16R5hkA3eCO+B7DbZCUr0QLefVj/G\nVoJnBMTGpzWU2ugWT36VvMMK5ElspZXMAepZH0VW6cf61jT9YMoxJ9yWZtbI0/6lnZHmYVjnltyei+mXeObtRB7nnaXSW1OD\nhE0zmNL5bEt4ox+0c3wjGaR/hT2oacE2M9IL7fMpBREmPU+mTwLuSySywmfNtwU6D8fuRe5qZBl5R6EJLcCPeFOnxQrzDwu1\nkYPauB+1mYPaXIQqFBo/6rBxP2pOqUN/qRasgJjPHZsyOWWo0c5UzTEDBAaPMcWdSR9MbUHhER4AJGYQmU2QqGaQMZ+USUvO\naCUTyPrGestIFfj7J/X0skAbIbFqTuYrejiQ/n/+0c+72HeW3F2QIGlkLbU88Ulg368d4F1SFkvTBuF73NQyFmb6CHRho99L\nRrAinN9l03gYvhndbwqQw5m84XJzSn6GJIKdXlrIfx6WWZZTsdhJ0j7lkFXDBaRRcR+Ecg0xp94pGJhSrSq+rMWNZ2fqG0RR\nDkuVvl2wi2hJduY3l/VgUzcx9KfapuUffetobK0qzsYqeDhWjxvObDgF3ibMNXbsnRhOvvJRfiNNXT6Px0Op/OGptnTM1oef\nqYTuzrKwJihHjKK0jLkB5ZzY1PWTwfBlNLuKw8aaThHxDarUmObUGovLGNRo3vFfwn/M/pBK8PtlXRN260xrE/O/RpVi8YPU\nLOZ/5F4Iz/k9igbClOwGiPVvpyVi8qFdoYy70eCQiUuqg1TWVuQLpn1AesUOEoqlp3FLNcdetFJfYceQEWjJRdlSuQ19ZRam\nd5aMBpL4Y7wiy4UClGzP5QVJtkGTZ0Nt/+SNngkWb3klPFWdydkD6r+hkmi5pFFNuqLuwkgPBjWFLFnzYz9V92sWNmru0Jm8\nZ4rkdkbYCcmR8pQOEtMMkCV9iUfR5SDubcN87EMmq8t2jf3mrMEHf5/xdrsqfr2DX58EoDjGrs1LuqCKIKomOUtU5OWMVqm8\nIDpTxZOQt3KSk7R3crazNK61ZySFMxRG981h9CVGmlC0G5L4yW0nXaPafWM2L8N8NYSzvsUPRsQQWTjWQLbWV6bqCQnr3JUk\nyMIywKemATby25JvYYz6ONXRbwAi3T1BV3zxbmllq/At+mMlfQoCtKwS3q0kT7tBugIJrCcgN8EP3glPu3PDvkrLEmbaiqoW\nEI9Xxk/pZBXTKU+xQN8tZLkoZkG2CiMCuEqCWAlSv6awjPbhhSAL6P2bLzmTK/wylUk+2aeBSmXrge5glc6WBTVGdHpfJveN\nVLZI6PVC58jlj8gN3wiX4EwaOEuGwsDuUGWqpcMUViaEWEGsjme6NB+taVjeqgaDNKyJE630KtdslnrXNO7admeXpsaQksNB\nFVrJVhiJrT4bREfJZBhNDuObeKCuUbi98HRUhOYepDi08IXpILrDU3m+aWG/A2nqXVS32JkHNVuImulHFC5qtBA10kaMLup4\nIepY3wK6qIOFqANtieCidheidqWM3LW6xZaRrlIv0q+TQQ/6GXXWboQmeO+JacKHcGq8Lxj3C9OSGBoYmwLKl0483oxKCQhy\nYdVZw+vMRPqnqzErzyC18lOZXxYAmQZAB2GYICAERkQB2JtYlV9mAGOriLEGYAgDK39A86FEFaUAqxSPaZXKZp3KbqXKZq18\nlbqvTuV7KlW+r1blBdXSEWKM6DB62lfyxtJ2AcR6NEh6BT2OChnL3C48WY1LOaOED9Gg69niyYFL3Iuq+N5khfUg8snyhQqn\n7SRwhM92OueX+d7xD1Lb3elKLWXZTXFuewke3ysLn2D2wZxvQZ8/tzfvX4JrnrrDd2/QMfsR3kmxJ/0qnVQSsvBN/20qtZAW\n/cDXdCLEUKgiYlbEg0tBJmvl5uDhN3s2bTCZwJBK8SEC74DiFN+W+8BqFljkB6tbYGM/WMMCG/jBmhZYF6dsXv1uvSTWLBIz\nX0n94DoYYgZp7SNSFHv5fnp01j4iz9/50vyrPvj63bM0U8MwKPtLbERP2sbLoVD7u3g+3b4FBLaY+9DVERqMJXGurHYS/UEy\neQedzMIZ8aCotkNmTGRxkUS2eqY3ZLFyZJUyrW288tn6fJlLhSMO9KY+yX6LL18eImu0bYE/fUZpWfjEwsKnNgf9HVYq9X/4\ndDkedcdo8U3t698oMecta7swnjAnTRK3cB1lhcs4HhXSeDKAydcrXN5JoK66+WRP1cl3qEsH+fUieX423R5J34piTHDvw310\npI9R3fiFRuVTGk3Oxd8Oy7kCxX3KTnVhp6t+48lzBCqfeOtciUZJNp6moGUbjJhF+i/m9XlRzryY2ilkuNlZ2/KGTBJTTIek\nApSAStyOR3O0s2gzz1td9D2FYeUEl2RhkKT5g2/y2ltmGB2hPx5YyyXqYYHIvgpJv+mxK+8Ltr9MZRW3xb0BrnDzeUCP+rc/\nPn78CP67idI7HIg3cbdRuDFPVFsCBLM8HqXxDJ/l9ZI0wA8YMc0CP7ss4OU4w34kjyuVd4xCUQLJu3ZGoIq35IVS5fbuM96W\nM+S54MC4c+dkLV4LoZdFfYvP7vnFHb8k/+i/klF3MOvFhWeX8VUyuuAt9LOdKfaUKlvwBv98DMzLEdmsojMKWTScDOK0vldQ\nndJauuE1A93xcDge/bygNUQ/qMYgzW23lKo9INUFh6/fAE4sOHx9UyS0FLhl6iCGZX2vqOsWaHK6C7GZUIAKi6Y1WPLWSsL/\n0Muk+AUfrW2z7SBMi30yQ58Eaky3UwwJri6cjZGcmHdYVock9vUVmnRv/zkNZKjs7X4yx02epF5RteH3pLApoRu8P0bcv82Y\nTlR9cKDncRj+mrJtuk6KR9wHcXoF6ro47C5VlGaKLh8I9DiI9ItE9eY2iPQTSJ0ogpLEUaojyehH3I7mp4x7Itg0R8/WWxGz\n6rHVDm61j3oHJ80Jt1zATL3yjZQ/qSvpYOpKOMIXZwWfEs+72t/k/mWqIgwkGYuyrKNActtlFW5vhOYa6P5fRt7yxAiyY59K\nHHUgruJnZU4EHkpGndTalFLDyzg3W97BiOGwmo+ilD9yexhz5SkzsyaApitzg7Z6KSZPPqKIemJKoGThh2l8RTOQJcMDXMty\nZL6gOvj6UkcmdKIr0roZFdBVjNVPTofwoc4Rak8puYG4RvAEkLLtzu0GXZEhOeTloOWAXkKVa3ScmV7m5/qZhuhSb6Wx3+LS\nqkGYoIrnjBauQ1o4viiVpRvWuVh/PKNJjiHndSlhKyj7yymV5urhC1rMmA9N0RhnMI2KUVSSh9f0EQA7iUEhVy0trgx/WYSA\nzyW3Im2buMPIwrJMd0a/2Z6lp0raZs+qX79mP9f4pZlF3WmRBKS2rnDGa2w9AM9jXzqFcvNBCupB+Qz2AMnPwFSCv6Y/V+fm\nM1Hd8zF5QMrEnLiy1anOYMlD6ZqyhsDrQ3HRks4TDbNlrUAqxrPjr1+jK08cEzk0TCZgzDhBVTJjDBkRNJN8SaSlYsrGReaG\ny/DP5zIOYK8YBS0HfcHRhhJMiRyKgj7NFQ9haM7sb7iQZ815mUivFwem94lk4FkaP0mXE5/QN5n4kcofmfwRiR/ydAtHSBa+\nJ7v0D3PnwtG44+EoylHP++oHtUKN39fExxQ/6uIjwY+G+Ejxo/lBvrKGjzXxEVnLhTHlRKkkgEIrAYUEoyZgsARlV8LAIMEJ\nycnCe9h3dhim883IejbPabBQH/Eg5lbyWZhCRdGzAlQRHSND5QL04tbAxzUpVChAnzlr+MAmfb/+IbiGPxvoVi19v/khOII/\nWx+CW0QHMhP8W8N3p/C3jk9N4S9QauNfIHWJf9eYfwh0FFExNZBBOQuuy91gUh4Gl+We6aE1wS6w4FcBfhXgVwF+1YGvu/AR\nwM8A/gjgOzZ8w+UH4MsAXwb4sgPfdOHHAN8H+FuAb5vwU3Fqj6PCZgvQVgFtFdBWTTQdx1eckLvo46Af3AY+tJwT6H0eBKCS\nN3buO3+emkNQS2V+ByeiH8dukCTxqjnOc8IQ5zphuFSOCiwnDJaYNR+ckue+U+INpTXNY2B6LwPThzAwN6OtWSvbZWIud2li\nSHMskTsA0547kA3hx76yUd2oVdc3Nmu1zfW15sY65i3gJViOCbnmekQUdwjKWAIRopQz6qCllYHkQq8sOGDRH4utLySlZ6nQ\nl36qiVH0k0dLWFZCUsczTKocZBWQRXJBu/25yjzm3FZut2MeFgWWnMqdhrhTEHcC4g4hPmuIzwris4D4HKROxQ6y0rOqqtlc\n1cz09vGAmrEWTD54tFZSjm7Bhy/F6pXK7/0ifzwuTe2ZFT7/nXCXbQo2LXI7HfYHcyvoyD/OpjujZCjilkXDGNZC0QTCA7kk\nACWhIECjVuJLcRGdgAVqmwfZdDyhZEaVLjbLwEJIGEIN4GNN63BMcTPgP8wYxC70Tnw7NfJGzOePqvHgCh91l4gYEefodaGM\n/BZHfxxFE9pG7CJBvvztCh+q6BGRx7pHl3CXd9OYm4ahi37YosYgY/lDDZDeo8plMhIuaIsz9MIMKewLnZAV0X/ANd7z2dFZ\ni9zl7S3OhH4hGfGhM+4bjrZLt1Dg/uHJTkctLgYsCUWKo7ALFWbotXU7sA0LIILUXu0c7l8QkjkLD2s37f5ou/Aa26Mg+XMK\n0JFueItXnoiFDct8fXx+8PK4vXdx/urkLKcqB7omiLIAkkROLRnUD47zaVOERXCbGuzFu047n4NNDwP3YOwOouEk7i1EXLo7\nRtlsMhmnGLGED7cCeqgs8MsQXPr7cun/wvO3j7ibWVBcYECjrWybK5bbfVbV84vT9tlF+7B91D7uBNI7bbcifgUZxvkdkumW\nFZl7DO3DdyamzxB+XfCTQuZ4B6bNrEK+mbMrY9708TZSeO2AFb5c++c/j4wICXJOnc8u2bTqwxp7jcErOBCaeZW+mG6jjxy3\n0SJGQHj0/vZDa/rcpdnjW+iVa0+DXMvcoCe8i2w/kACeabEGKmpK3F+JpIgeVdnp5WvSWMXSXLTMT7xlij7Wh8JULod3mS0b\n2cf8YhKae0KL/9BeZVQforXGzCPz9OiJil21BHYX+vOGGndBIDOfs0GCG3s07lGExkhoeQrKBYik1JpB9qjCo9FKOS4aB8vj\nGQBIyhzwdUOI3JwwYjglVCHFn66/fr2Wk+mZmlYl9GeGWmM3kPO0Kwrn07XLL/XsOduV20Lma92ZrXMdgOehLdPXTKMUI+Eg\nBJ980dTbnb6nVl9YOooMGLRiWSULaWlZMYfRl5AKs5yB30LUKadf/50VGG0lA8ewAuEL6SF6+WQo3HlNNGB0KoWzGP6yUI4W\nKRbyCVGVYMU1LINqiO5gRgOqsqFua6k/4a15FHD7r+1xwCXe9kCaAqSDJd1+eE0Q2THaQxx+mI4+EtvRRzpXx5nx0zp6aIN/\nx9R7R4JehExvH91wjMEPwsEqOl+Ln8JmGPAGIPHf4/nCezxdeI9HC+RNNnWvr9wu9Vauy5ECaQNI+1m31dYgl2F7pV/OWkf8\n+fZlUO7gpupW+ILk1l0T/tV+Ola/a+Xe00FJO27gpQ9Y6TKtA2mdZ+NWRxfXDjur3ZVecMn+Fnvok+EYftfU15746rWGotTg\nMthDiScYPIavuekEZLicE5CjJZ2A3C7hBGSCTkD+p30/pAOf74fFPh+gj/AZfPcq/PhfSb8Xg4503r7YOTx9tfNq5/zV40dJ\nv4BXu/3+LIvZPW4lKjwroKnXYHIdvYqy68416JvX40GvWLhRD+dL8H+wEetGaa/1+L/w2rT/MZjlliJkHuq0BZZ8gekX57s7\nh+1CiBf9a63Hj3j+NZSJV8jsLprdtzLTAWU20E+j7pS9qI+bhRWQNyP42KhU0XYAoSu3hVWgWFPfd4C/ArXEtNVCdJkVBVKD\nIN1BlkQv8f8DjuaUqQZnquFjijJd1F+cIDAQiN+fbcJ5LY3lTHRrY1EcAfbde/i8HZoNn5LgXbq4Civ09nu32sCBm01A0RbI\nnQsCEMiUKGGS3LJrEigB/RQ8haZz+mxF88EwWWdJxIy7Pagz5uLbCbQIl22FwfgKPlQBJcUfh+rGycAPJDlk5UTYYLQM2Tui\nFMVHBQ1KvO2Ri3HnwTCbZxCnk/0IVxRgQQxGl2UNfwtgbLgWyhQXxySrCBuvJEMm32kSWFl8dUTAAg9F0UCNQjfKZCc0WANh\nO9yynqyzMR+xCcEpRKpRigBThmmyxiBKDF7BMAj+VVTpt2Jq6a8FZdgNOZXDnTXQLYgdAxzLe87KwixMes4rBg22LX7dcTD5\n+bmlZ2QXt4FFUghrsbi8rrxvwCSUkqvvkVxHO6ePH1mSccUwcmH9dBRNYHLviJ+vYT5UrrRI7OURds2BJDWNfO1Bhh1DJ09q\nMwqdGCStRzhPcolJVsRYk0Q06tBCPWE1EODDywQWmpNudzDLuKFR0WijsWigsWydFDqY9QCOEUxFvXkEMw5FMGRAx6RxfwCL\nZdw7xLUM3WYw65s9XmHsBbtYQAIOC/xdO8hP5HP3sL1ztnuy04GiYFywLV8XeD6fxF208zsQZPPosdp76J6/arePOc3sOo5H\n30uvffwGGhQI/vOfOuO8s3O8t3O2xwY4b+reeHqMRllZBI2LF52YUizIQ3x+Dxuob3wPt5ekUhjltKlkHpkWR/0ySTFf5EUH\nTq2CgjI8Ssezq+tRDAp4SddVDqAr7wDyTIExG//2iDQGiR6WFybVFzud3VcHxy8fP9LRYzmBS9ylHPQI4WtogwkpmEHAdkWY\nfgIks1yExfmFyBE3QQVxfTgSpBOxNiejKd9BKQFxziztLMJBoYr2jC2B8W8AxyNsRmal0JTpuGT8u/D/GEWZdsfSnqo0tJMs\n3NR4gfFgP4aCPOUlXCO5DQpMC6mqVQKQ68si48T0EWg8gEDdR6D5AAINk4AQ89hPoC4BezdQwg0AAVFTuN/kjRPWx5dGBwM/\nnk4XAwjpSqK36PjJMIONe3LFJYobIHgV4hvigsqDIwo5l4Xwp3h8hisMEQvYLAPExsv2MUh1gieiDitEEWRX2tXK0o6gND6q\nX168GCSj0en1eHR1cQCTJOkmMErZUBftXq3UQWWfP+YIewTBmSMZNCV6G8kCOwfFyiuD7Fl79+D07AR0zIvTA6ZAKGz4QtVk\nVS0ek/EnLpleBQQK3Wo9ZpV/cba37+eLZTM3JXtoZ2ym33C5aScLyykrNROyki3CQW7NeR0ZxnU06KNYNgxyJS+4CZFiW22I\neCs5cl8yJAmWTJQ3HhRVNROH8bUP4PsX591r6Ow/inbFmP8yTlWX8hJQ8oaKAtoDIHN4kPEgBkBL9f8+6/OX8O8e68nC06cF\ndvEOleh9DNrmVD44O9hrn++2j3fbco8Jc7pRePvu94vOyQUMpo3qFtsoTZkeXGhU6s1qc60JoqJcrWytb9XX16FibAO6tt5s\nNFHLLdcqa42NWmNzDXJqlc2N9Wqtuskw6tVmtb62xaCqlebW5lqj1uT4zRpQqDKM6tpGvb5Wf/xINy4ooaN4UO2MD3C7wdL6\nIk2sIHw4/ZlO96vYcRiPnYAQiVcUs5hPg1WJw1VvmlVWWXzDyUqA8jtjyY7ghImwYTKFJj7AzhZL26ib9EBMHLDNCtnrwryr\no1puokFxvHATj7Nlga7mgBo7Y4tTsWf4QayaGF4WDRDahDEMyHOmjCQ3oJBI1k5O9wIhFK6T/tTYtINKnaFuwHdGTLIBOPzL\ntiRbauN2Q8T7WqW5ubYel2uwpjUrzXq1xn+vwRjeZL/Jjg9WD4VYq6xv1qrxapVtdTa21hr8dx2G72YTfxNEPB2SiM1Ko76x\nCQBbQWGr0qg21/nv9cp6rV7D3wQRF4+QMbwiRiupHFJFEd0dZ2xlQ2nNmmBVNc5KAfb8RegK3kc8uyRxsRgoAffGIfCy0dyA\ndqo1fWU1K2v1zTrnThYJVW1sbbGqWiW/B/3hAy2eoK9YvEguCk/ZScj65lpc3lANkF5dQgOYomYFm4XMVYCxBs4BhknNuhiN\nRg6c8WyKLxAOTs7keI6nUV3+hvp0riGhJhOm1+w9wLBzDYKai1FG/RKY3jekyYHeaye6WCiHHSeAXkNL5mVmw/F4ep1N40lR\nuauEMeeUaR5xZMmI8Vg//xNdarJG1LRhelnF00MDDq1qaRKWyZwwRyCFMd0Yt8IU7pl4N4TNIPuBiEXEmbsFKJlLSRFOzlAq\nWyLJrFVAq0wxa3VzaT2rivWUVFqDd2CqqboCLjmXu04YqSrbI/OKWw37zOBBYZweGFRYATB3yhxAjWgcQXx0WOuVOEvhI0z5\nMd2C/1h3CfSzmttEgmTgjABV6Fm9YTVPzWqeOpVzwH1DCayq7FDWFqIsPsOfuWOO4fJc2SQmXm0hXi0Xr74Qr07w1Eqh1gIL\na8Uz1VZ0S9B20DsH1qOoCrASdcvWWFOJzsNhuIKtDa0bl9d0B2qBxuH5NGDIpJcyNbHZAC1xWh6dw0LcxXmDZa8CDUw9gO/d\nqs4fYn4GiEiXtRGst6gagC6H86AFf55Ba7UKq6vwU8xrQFsJGceIwkmdD1Wr+pfoIsNf4Su1kYLNyXgG9mDBQepATooKtdUE\nkXkQ0LFn7TTvrJ3m66PTnJOOy9lw4jvrwHR2hsw1x3qh96p3e3fR/9QrKsEOieedHm5V+YH/zQtO7DU9i0eQOw5y54Lwwl4N\nUNtQReLg0wd3gsPAwOVHF2KP8eL2Ydirgm+gAt0NhVNad99C686kpYU9DFNgL2B0qeo2iVMAuRTPmNJLoQBns7R/AWpKQD5x\nexUUdB/I5ReDTZCXk3q1vTlProbRW3Njx3tI0pcXCi0T552Dc7cI5xgnqWSRzHac6+xhlyQbICyVtATiOFAMkxHR34vxmIBv\nFnl2gJRxjhgV19y8TCM8+GC3mhyfr+6s0dgFzBleu/HPOyY2jO0MqTi7mlMkVBVxZ8FKMSfbJZ9shePXRxe7hwenpwfHLy9O\nD3eO2+eFnwvVx/yIiRmU4onkBN98RoXZKB0PBheD8XhywcyCHhOBgzK12oI/zwqvjw9Ojm26mAVSiHc6o8zkK/dresrfIwDI\nB7VE8IbcBYhT9eyY4Yle/Vl8faIn9jhafexCvfnpsZc34NnTEsDI5Xg84EyyE6ppOmPnhwsaxGqR3Kbwlmi00X2N9Ejz9bDG\n+uc/ZZWkkM5tMaEhioJoO1un1J17B5TxQJsySuQ4G3V2dX0kP+iTt53vKVkS+XQvEYoHLV4uDNUxZIXtWySpM0HKvE45OTw5\n42eYeClBb59wLb5hv5DGwItp41Rw/2Tg8bJ3lyibNEdzqYKt9jNLPP3xJRa+fjXTD47xQmcXu2Q5pg6XYcqJMcCPc7+PK0K0\nQYjyqaKVG4agwNntEakAhbRLkUhsWq+EyoBXJepW2MNW4CzjhqVRqTVra1v19bXG2ubWxlbjsc6sF9Yr9c1GbXOtUd2obWyt\nba6T3As0yIbKrG1UN7bWG/X1ja3m5ta6BjEPnqsVINSobm1ursPfja3qhh+wjkYla1u1teZWE8Brm1vAmQJtn54fHJ4co769\njg0ywhaRR7IKSp/R4h230NYjGihCtanA+HSdoNMn1CXRyIcjyv20Sa4kDujlLts4nmY+OQpiHb5duW0VxMm5Bc3PezzAinbj\nXtoWQjMHQfxGTYWhSAL1ldu6JgCaeMPm74YWyFR19s9NBdTAmwo3wUHzG00luolhyYgXEuJrktb5G/w/ppABIX6PyMml0ahn\n0qoXZjd8IRSGqgQaTTpq9crW5tZmUMCDpI3NSr0Bm7Mu/G42NtY2K2vNNdzdUKye0s5mULG7QOi6UXDJjDgKGcr14Rj46E0D\nnDHkaF2YqjDTp2zEz8r4bYmYq68OXr66OMXxfQ4LvjyGnYDGl+Bl73nUF3alRU9DSQOjG94w/4Vmq0uSoKZN6hUfN29qCKXw\nRt9iW6U9NbFK3DJJJRCN8TE3BC0ciHNddg+urmWE9LLcpkACU59ugP1L3JTNW5LMmXGfrugYlgomPXmvLlMtswY7WYPPc28R\njRXEVgkef7Obnm/10CPER8JdsHZ+VMlOgSsKRZfMLn5YZdEByhHe+OjJyDLF4QEHHOLeG/6V50F8gg/ZJ4qMITvwEb/q+IsN\nQIZRczHuFMadwrjTGHUX47PC+KwwPtP5yliUsmowGyajiB0am9IKtSgiYljaJ2YCmpFjsnoNVrz6FrbfRm2ttlavs6bcqNc2\n1qq0UCZbBH7AiSMTbBIYnt/l1TjvQKPveGXeFxpY6ZBb+4ixUTc8CpHBwAnwus6Acea2GzOwdfAPtzSzFl+089SGo2gSGTHx\nJhZRxIPuKPPjRH5A5VBRRIzTg1nAJJlxlXwYDS9jfkRMe8BQhBfdZVNARZkcfJpk+1XnOrm/VfVdob95RVtP3Bmi8yZmU4l3\nHGuVtbXmRgN5YOBl0JW2QFdaZ82hr3XlSlEl5/OSHp4vFpEFPAqQaXqA+uohGPpfUhHrqvm1PsvjBloXnXenoPa+ftG+eP0G\nNo9CGevOLuPXby5geh4l3CdmoVmp+vI7yYBZBRVq6wig7H+Zi82C5a+LHCzBCkidmrEF0XDGpU5vgFBBTTix8aXYMIt+NhM+\ny436ErDC2PKRKKZnQKJgfs7+3QY1nRX/iOsAPow7hVFjGE2OMS+w1y45DH1emqHPinydkV/7ZoaI2TmgqJPEOvbb6zd2r9Ez\nwgI5r53dqO7gJYf0jmp2I22ZaRUCgzu8ozY7XixIss0M4jUv8TJFDozPz74C7vILqC9TwD38f84n37iX/Hc1T/Nh3C/ROPn9\nuHw76KHGLbBRx5fWTuQI+zLBUHtRusvkSpHcKcSjG35M7h2TwwRtKA19W8wCJYL8QoW5njuQGnnRJ/DKgnqgb+REcQJHZntw\nLfl1zo0ouciXPCMI3wbJXVUoJyCtJ591K6KnGaWyGKqrhkxkkD+boxhfgIQKkYkLBlYOpTzDHsJtl4QTZ+ISXGWpBlsp8Bcm\n7hIg4FmRzQpfpXiVcX0B2KOdtxdHB6fsRk1VpqSKWQklYKf9tn148dvBXueVImrnvmrDzq7DDn35gqYuV/BYvf22Q27K7ayi\nGlazG7ndZJ1sfKBZNzN1ePRfUsba9CghCs6NoufOgplWscvcdbTK+9SFrwErmy58ja+HLnwT4JsufNNPfQ03/9U1F34NO9mF\nX2datwu+zrUDcWQgjaY7Y5gP0h6EmFLTzXEyse7+NdzPIWkFMaI5fJE0Z9mgvKLzWBvp9uX2UBSPEl/VkLZ89TLUzGOotoCh\nJmGoZjFEmGXENUO1pRhay2OouYChNcJQ02KIMMuIa4aaSzG0nsfQ2gKG1glDaxZDhFlGXDO0Zq9ZvMiyuDbnz5hqlRqaTVGL\nfvMePJmo9agpJ/g9yxFPZran9w9ysX2zZwdB0SsJbCRskdmi5Pb1Iy0kbmbyFUq8AVPZ+jwIbRfs9VZWi9SILlOsm3m5od8Q\niR19IvGAGCSRLtHF1x5UvFYVrNKG/N0CL5ITDjiLpcCwidJ3qwce63ZlfU6N0RfYnhNUbYBuGKT7LgsM63zQdobSfNc21Gdn\nKw53T+WZBzvIuORnLYH8gRVWGTWZUbMy6jKjziwF/QWFyNqK2z78IZLTIo+8rWGTUO1iXk36L1NECyW6heRFygNaKJEtlFgt\nlMgWSqwWSmQLJfe2UPL9LZQs2UK+4kfEMaWfEcHH/uHB6cX5wV57z1+PshfX6RpVFW9NirlxxV1w42BVtxfl019IeYm2+hgc\nm2ZKewfnp4c7u8wDSY65ErqVxmAEeN3kM1ui+dJ8KR/iRRJl+o5v/x5u6AObVcNQxngVw5dHom5aPMNCtGemcIMmdjBncY/2\nKha/9KHMiclx++jg/PzgTZtxy9bEeJhkGQZQcR2jyyzOUVt/CdOs6XgaDWTyWdRLcEKjRm/Q5Oqz5OfFAn7cviQcaBKfr8In\nljd3vu50xiez6WSGoRviQbFgwJRaT4JXUPZj8vTi8OC4vXN2cX728gXaRYtP3qnvLk4b+jWGPJverNeb6/UajvnaxsZaY1OM\n/kCDVBuN2laTgWytr29W13wwtY3qZn1DnGs3tjaaDLxWXauhleNjaFyXTc0XYRZ5t9msVYDLrWa1FrAX0vyjaXNRZg9B6tW1\n9S22suLPjZoXqra13thY59SqG5vrjXVu9roFdWiscX7ZWDpk3YA8dcb8Nx/Gd6cNeYLepB4JjKWfOx1glhb39ox0URCpc29Z\nuCpQcoA0vqnw3PbOL5zfIsXpSbuzv7BMFsFc4mbYXstiai2JPSlTfOtr32ZtfX1d3B7g+5419dqFvRzS/h14bfFity6xB6jB\nXkejNvoS9lKvVhu1RnVT+p1Y0Bai/ZdvB4mX3dNjbnsJGKD0MXhpCRh2Hq6WJfi8+O3k7HDv9ORceyDAeF5oCR5daWP17CSd\nXo+v0mhynXSlZkxBrTAVYrrcqDWT6ShC/zDTaznpdZEuL5Cppr2gZOYiVNlKlUV9TskbTb4l4vdtCKy1nvzbT/lkTzPIKZn3\nDEcne+2Ls/b+YXu3wy7ipX02v3F+E6OJgPgoGi0dGKyUjPMYLwm+McolEUgY4P4MHUdKmuIlu6Dtkr4Rt+Pms3ffXYp8Cgz7\nGmu1xB1P0dhF4gFJMmmzFNygqhLxlJV8KYNZwqBRAp/x8qzSz+GLw/bxHtosHb0+7BycHr4DOuPZ9GoMWxB+5a8f0ejEwPxE\nU/SRtnYK1LPK82nKTBh0NZihuuSaGnA5/By8ZfceyzDzwwvf2dvzFA5qGS0JjXcXF+XqpG/8IsZUIvlgIK4ArHw9PFqLBpyl\nE+FAE6TJmPEoThqEDxjF/B/LMG/X3zbSE68FPGZ6xydnRzuHbt7pq5Pjl1ba4c7Ri/ZZh3WSskMzBLQ73A3p7QYhIkaubpWo\ndKAzziDjSAPZcL/lLix/x7a5t3nuq/0SzWc0z9sHrbvmmhVyQW4a+JoiW8r9NyxY0pLLNAf+zyzUvrKdWv6w1drZ9H/Lwi06\n37di89osXLMNbHOx9mDfu1wbg+tPc3Dtn7yEDcr++GoPAxza5uHEJvedD08MeGGII6lopH97kOSpxsnLi/bb07q+BhxfKf9a\n3HRDPNXtI1nuKWjF/NB8099UBXBJ06eukH6MAbo5APy1ifAWpLvfCn//y1ZdO53REZ4TdJHkBOEfnuYgDwgaCr+1oG09DWgt\nh6qJPOuaAsGKt3wZ+yzdGjW/mpy/PNvZO8g/N7pCj/jyzIhaJV7F05ci7yBNxTFH0fS0YXjpoGYx2i3GoTSHlTg2uHxGx8I2\n6Ot5hir9icC/+uI6r27mS2JyoEOqGIhi0MeVpX7WC/1PeNXAvDUWJRxngXqzwdEkt4Ub4pJVWuHbr7M3cEp8Qt0Xf6+K34w0\nd2Koew/2cL+bHXeIV8L6xIq1G1SBHfOYJ1YyC3g5FD/FWZXuINTKVDeysyNCTmyKFSBR3+5x9LUaesjrWfRLXpXcgSjJOCqj\nhytJ/l9AXtj8HQk3V8rfVesxCblH7P/Cgv3GhUA6SnHo6Mmtj0F8EX40FQg8M9ZahrA/tjnTBsjUylA+0LeLQZti5nD4rH3B\np4vHvNG0lOZdIzYWpq2idDmm323584+9PnQsh2V52bvSf5tDJa+T0MIYNiq2qbY56Hwi5R4Pa6QlKtRiT82JhM4FKWwolrQ2\nt8a/M/oJoRXLCNU//viJkejYA3M65Rmw6kL+7/Xr/QLm25pYvwYS0wetCp25RMGszmDQOR30MZiCDJByitleA1Sc3MQYsHT8\nqfXY0BWEVz5WQ/mKzN5AMdl4cXp28qJ9jvsfgwCTgafp+DJ+X9jS7xIfC2c2L2OyRu9Mi45LLOUM63p3HPf7SRfZyRgtOru0\n4l5h7ujU111Q+Ky/PstZlMbZbIC6r00X7ylx5dzcXK/XN9g0YpCrLmiNgXLrBlhqa7X1dXQic7cQqe5H+rwQqeFHul2I1DSQ\nmvWtarPBXLXex+KaH/HuXjbXGSJTH5qNWh0NDT+z/7FrjebGBp5DL6Swkc/z4qI3RbcplGKB+6UtM77vqOU2p6Ks5EFdPFSD\nlCqMHpdueiD7XbgRX2zfe3DrCnvPbDE2aJpDWtuEKDe6xjtkXufX2Zn9pH4Gax4x4S9fGabviUhLO1PQjWYRbwrLbF8o3BzQ\nMervzqbjfj83uxd3o7v2rXq79cU4+oHxcdh+ubP7jksvJrfEY26DrDDdZt5TKb2fPfY37IJHL+9lk//CU5v0qlS9LVYti11u\nXGruNWWcqv1oMACSyps2M4hlfFhtZxXB9iQ1YlXkrbWonl3YivTWouuq3C01i/fVuqQOYXQVrRLI3kIPmPPJeLposMAnLKIZ\ndK4zFCaANLxMo5zsaHQ1EKjmvRXZEVHqNjkDX7x5ZC/l9w7O5PBij+SFdq3mfjRQ7/vct4Hm08E52Vw4+D0rQTgFIMWjQwSm\nubGZZ0IfjPpj0pr3UmfXEJb+PtBa0aOB1kLpMwiBrdRTAUejyDuwRntwePFaUjubmBNrGVbt05OD446v3VlIN6vFJ+T82Whw\na6KRFJxJVo8QyhP1U/SCwQ7tB41k9YCX2v3q8+Je4WsYD07M3C1JwpWJvom0ifq7ifoWFQSpC3tj+ofqOa0Bao4Rwow1OrrS\nAUDeimGJOUKp503EzqMs6OEkaIkifwpzXTGpgXZ+euIdZyip7htm+TP93oH3yJB3xPmaFkvW8NQMZfKXGJy0CnRsKgxraHoo\n/eCBqej+6HEppbQ81bPwA1Ky961KZq4//JWIsyhpIqJ/KF3SQ3rVIIuwXYSxCj9gdlnTi/LEppNdkIOz/ISjjeZJ09Pt4fPN\nuKUxa2R56HNo96MBe/NODY/VtD1r73Yuds7aO765ewbNuJPGkTV/1eT0z2Z0efwbnr4aKa/YU2drLpJjw2n3otbKyagTFJOn\nlH6JaexU6UPLrvWr9tGBr8Kv0GSPBaK9VxfJ/rjbNdrhKsXovLseFcWmem1+C7YpTx/klgcGnoXt3Z/4SvDKI7oxW3S6rwj4\npz9mi6OU32JhFcHf0nFq8qW3Z3PEDt41edJqtFTZuoGnKOqegu6niFX96MJ7nSub9ODFYf42z2iiRe+Sf5RNkjKaOYomHssc\n+hbB2OE6jxDY23dCR1wHuLYcztsxS4SQt2Kkyc5yGmxZN+7+5yhLtLDfHKusCx6RK91HBjRZBdnA05mBYlBzRJ7ilHzUFnQu\n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\nxhh62liT85yy/NEqFiGuC3p6KZLSxFLOUimnRAsu8j5VHkzgQRI26BSlN95hmRwo1XKPWgUWOvVTb5F5fAW9SIcfZj7Mqfho\nqrnzw3Pb3Up62kcDJM5fGyyJiG8pQByImaXI8WBLKR7/OqOuXVtFYSY8e4vPLPF6PBhlllEfdtJKKQ812co1iBfwQ8NSskGG\nz7mfWXa70VzdksHUdcb7idcX7ndbaSMUu1ZaD7Ru5amQDhtvh0EcJlE4uswsrz8UpRdfUx6JmlE6m++2l7Lu8gBUBrbZnDFf\nftIRbWqvPs3EYDh4M3Hpuy32i8+Nxk5rbXdrd9+C6qDn+1Qrz0AwjUkEyWwG6YcDdQzq4b9pZbMboqr22Wg2d5qNnYPN5tEU\nVJtDUMVgLmQLc8qdrz2DaDjyxRZV3awwtTlnrcv6EBitSBGCKbVYReWzEz/0zKDROphG4i1mDQ2ZxyaNbIy3nVY0tH02q2Sj\nvdW8o5MaTqfzFrOShu+UTkrZGG87a1lf7pyCd4ZaXd882NtaBRXY2LFwthPEo77X9tEwm1rLug54izlUq8eXU2h2HXecZ3Ux\nVqbZKfJ8x8lYq0J9hSS7ijvP2LpUqhP2FOm867yuVaK+/Zhdxz0nf3tNO7Pl+HvtBHvF+7fqtDtYFeb0rVUbq7N3dnX3nOTT\nVd2udbc0B7JeyiTV6Y9kZtf1fWZVVpXNW02odzPDbFcOKGel7TOFp/cwkKy13EJavseSstapGlKz6/0vMbvC+5pdh+fi+TDc\nK6qQKzvZy3J6DAudFqrpRZ1YgALjrh1+oEHVshApa3ZpTxGUKQNM4OVlZiJXlvbSqqJh8EwzTCDnZWZTzjwAzLyiNId2gndv\ng1C6CIR9RS/DGeaYQMsKzESsrmfVN54RedoqE+jlI+mzKkgZZmkbi762nWGiiSoNRDMr1j0dmtFFKrTZaqIypfDMigy3iG58\n0RFgs9rkUFDKz6zMWA8bL9RhZVb7TVSmlp8txobTxbDHqFhbTTkp3iqKmRUaXhrdNqPnm21GnahNLX/7yrRBYH3xXKt5J3tY\nWFDegQyjbzOsNZ2Y/am9bcc9k6SUS8k0dwgNGbaSqFzHcpdKTS9UpsVj0mE1oGwEaTXMJC3lyzIsW0KG3SgWles4blmlIREW\nC1dWPV0S0vhmk6D5zfR7sqRWiwEp65OFb12RxuGUNajVOYXPJrLbV2845+yGoUaG1cJMk6IinklOysq0HKJX9oJS9qao3kA0\n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\nastN0Rgkd3PVRY/6V3BQ2z08yem477l40JtDAadKT7xfDdYFO6Fdc559ZsKUdqSvVXX+wj2afTcS30f392ssyfjj4WlaMmJp\nNqo5S9SnlB06pLCMdjKBHM/Yz8SRjuUwR87KIski7XWHX9i3dItX6nU7gz6plEwOyZAUlB4T/xmjW0Y7il/k+VJvqFyomKWk\nmz9wJ5yR22tL/ipXXcnNtNN1jGiBq0p94g6ps3R1e4ES8WhL1RE/n6F66Iejr6hwiEc5qdvMXU4n6MGKsOZQhwCZ00ELfVnp\nB+l7dAmNq8x4rPpz9cnKgIi7A41ZVXd3DKKPwAHWNsEPE8e2wnn9cpNY64JZpnAp8OOyKXvK6KdatbhR6g3MU5v28t/WtDdP\nH69B9GdbNX9qq8zLpEfaZl7+3Z1tk+H8UeWwUmqIs9aA7Ndrpf1DvNislHcr4uyQQParR0d11D9IWLo5ujvbmmCeOuCIs3/2\n2OfFfvUQ30SZ3HqckSuPROIsoqr1V6dvv1J4PozSmepkRfdYZ0d9WdGnCqCq0oe6kTzblogrx/B7qy1/43to6CblqI8T+thV\nXzX5Jd9gi7OyRK9eWzP7/Q/6PIziE74CcV/hzdgzrc9H0OhjMY7gnEBbxXn61V4KvmHhGwCfn6Ue5s2yH+ah2PEI7iOL++iR\ntrAXhFMsY/atx8rZt4qynN7b0H/5U0YrQZpPGrFUmR88amn8HUf32mG6V+9J2mSmQB7Ek9wwvolGubidm9zEBZ0pq8iFg5aT\n0siNr+Jpr5W7jHLoWhakH1nFdg8E6UZcaXWizyN5/Yaq5yfOcjnPJtl78iRTMSO/4Paf1Px0EpqfHzc+Bu+iEcGoYdF4ooBg\nS5XJNYrqJFMpfFKH9muA3VGrnOTkxf3fru8flnAc6mU02dCfR5UdJBEFtXwIdRm0HUFrzlQTYL0IdbOIajoTAYtafWHEw25T\nRaeyEg9T7i0rJZU6ZsqVYsudrXqcnL8PI4RRHgt7XVQlPiFK1/r9faykN1P4j1Fm4QXRt4zHJ2izlY1/X0dHTFJpgqqY6Qjt\n/ktWdk5NJOzHYo/R/+lF6aB6XG8c1Q+/0IM4qY0ECZbXI0ZYyX54a1HnPQ9PPovrDjgCbrXWU8ICHpVu8KZ1lqlag6a7GS/W\ngFCXeGQ7NLd2e1C31FoV5ZlRaq72vGKdo6+jJYiMzbIn6qb0bnCtdFC7JKNYFZd3Vz/bPXdQ4Pedie2afOoujDbPX3tInytX\n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\n"""
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
