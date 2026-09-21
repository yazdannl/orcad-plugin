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
_EMBEDDED_FRONTEND_GZIP = b"""\n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\nGrTZ2ZgCgAs80GVXPNBjN9AvQItfQ8Qjl72BIrgmntIvLEPEyENEQZ8Uzw6B2KXJxfk2C0IeqqPIq59ss0WkpCbqy/Y2u4rN\nb5UtiPH2j4eXCloGsUkw7jeHAAjCCODn0Ov3Bk12NPa60PUXgPSIYPjTZm/xp8N+mMJPlz3Hrx57hT999hp/Buz7OZV7CV3t\nNjsd9t3Y24ZObgXpNnuG6N/usr+AkED3wxgpgbtLF+Nbx8lt2Vubc5vNwrRxFom41DOO/Pmk27WjrvZyabvhCVmVVGXpG4Xz\nMaAkXTJH2EGhWJLaBxYV+JCShiE139C24k1jvjnSfSNUY1xi5NpC/Ml6w5viQxqg34LsfOLpMHmU94Kb0hYs699NBYeUyBNt\nypA1sq8Kg89oSVhsCV0anErJC9mcQFxvUX7VjVC+92nRC27OJVH2dx77ZIMHjdjwYxoCQPZdQeTmraUgSeadbDdhF9xuuvin\nhX/a+KeDf7r4p4d/+vhngH+G+MfHP+f4Z4x/JvgnwD9T+OMiPBfhuQjPRXguwnMRnovwXITnIjwX4bkIz0V4LsJzEZ6L8FyE\n10J4LYTXQngthNdCeC2E10J4LYTXQngthNdCeC2E10J4LYTXQngthNdGeG2E10Z4bYTXRnhthNdGeG2E10Z4bYTXRnhthNdG\neG2E10Z4bYTXQXgdhNdBeB2E10F4HYTXQXgdhNdBeB2E10F4HYTXQXgdhNdBeB2E10V4XYTXRXhdhNdFeF2E10V4XYTXRXhd\nhNdFeF2E10V4XYTXRXhdhIdcznYP4fUQXg/h9RBeD+H1EF4P4fUQXg/h9RBeD+H1EF4P4fUQXg/h9RFeH+H1EV4f4fURXh/h\n9RFeH+H1EV4f4fURXh/h9RFeH+H1EV4f4Q0Q3gDhDRDeAOENEN4A4Q0Q3gDhDRDeAOENEN4A4Q0Q3gDhDRDeAOENEd4Q4Q0R\n3hDhDRHeEOENEd4Q4Q0R3hDhDRHeEOENEd4Q4Q0R3hDh+QjPR3g+wvMRno/wfITnIzwf4fkIz0d4PsLzEZ6P8HyE5yM8H+Gd\nI7xzhHeO8M4R3jnCO0d45wjvHOGdI7xzhHeO8M4R3jnCO0d45wjvHOGNEd4Y4Y0R3hjhjRHeGOGNEd4Y4Y0R3hjhjRHeGOGN\nEd4Y4Y0R3hjhTRDeBOFNEN4E4U0Q3gThTRDeBOFNEN4E4U0Q3gThTRDeBOFNEN4E4QUIL0B4AcILEF6A8AKEFyC8AOEFCC9A\n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\n"""
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
