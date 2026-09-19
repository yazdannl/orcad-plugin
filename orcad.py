# /// script
# requires-python = ">=3.12"
# dependencies = ["build123d", "numpy"]
#
# [tool.orcaslicer.plugin]
# name = "orcad"
# description = "build123d CAD tab for OrcaSlicer: searchable parametric objects incl. Gridfinity bins/baseplates, Monaco editor, live 3D preview, STL/STEP/3MF export."
# author = "orcad"
# version = "0.3.0"
# ///
"""orcad — build123d CAD tab (Pages capability).

Top-level "CAD" tab next to Prepare/Preview/Device/Project (same mechanism
as a FilamentHub-style tab): implemented as orca.pages.PagesPluginCapabilityBase.

Layout (v0.3):
- Left: tabbed panel switching between "Objects" (searchable predefined-object
  dropdown + styled parameter sliders) and "Code Editor" (Monaco, textarea
  fallback when the CDN is unreachable).
- Right: persistent 3D preview (always visible) + result + log.
- Predefined objects: Box, Cylinder, Tube, Bracket, Gridfinity Bin,
  Gridfinity Baseplate. Gridfinity geometry follows the public spec
  (42mm grid, 7mm height units, 0.5 tolerance, stacking foot + optional lip
  and magnet holes) with a simplified stepped foot/lip profile.
- Exports go to <plugin_dir>/exports (inside Orca data_dir, no audit prompt).
  Manual import into plater (orca.host is read-only): drag the exported file
  onto the plater.

Styling is 100% inline (no CSS framework CDN): Orca's WebView does not
reliably load external stylesheets, so the modern dark/light adaptive theme
ships in the page. Only the Monaco editor loads from CDN, with an automatic
plain-textarea fallback.

Tested target: OrcaSlicer Nightly / >2.4.2 with `orca.pages` (main branch).
On older builds without orca.pages, falls back to a Script capability that
shows an upgrade message.
"""

import datetime
import json
import re
import threading
import traceback
from pathlib import Path

try:
    import orca  # provided by OrcaSlicer embedded interpreter
except ImportError:  # pragma: no cover - allows unit tests without Orca
    orca = None

PLUGIN_VERSION = "0.3.0"
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

PRIMITIVES = {
    "box": {
        "label": "Box",
        "blurb": "Simple centered block.",
        "params": [
            {"key": "L", "label": "Length", "unit": "mm", "ptype": "number", "min": 1, "max": 300, "step": 0.5, "default": 20},
            {"key": "W", "label": "Width", "unit": "mm", "ptype": "number", "min": 1, "max": 300, "step": 0.5, "default": 20},
            {"key": "H", "label": "Height", "unit": "mm", "ptype": "number", "min": 1, "max": 300, "step": 0.5, "default": 20},
        ],
    },
    "cylinder": {
        "label": "Cylinder",
        "blurb": "Round post or puck.",
        "params": [
            {"key": "R", "label": "Radius", "unit": "mm", "ptype": "number", "min": 0.5, "max": 150, "step": 0.5, "default": 10},
            {"key": "H", "label": "Height", "unit": "mm", "ptype": "number", "min": 1, "max": 300, "step": 0.5, "default": 20},
        ],
    },
    "tube": {
        "label": "Tube",
        "blurb": "Hollow cylinder.",
        "params": [
            {"key": "R_OUT", "label": "Outer radius", "unit": "mm", "ptype": "number", "min": 1, "max": 150, "step": 0.5, "default": 12},
            {"key": "R_IN", "label": "Inner radius", "unit": "mm", "ptype": "number", "min": 0.5, "max": 149, "step": 0.5, "default": 8},
            {"key": "H", "label": "Height", "unit": "mm", "ptype": "number", "min": 1, "max": 300, "step": 0.5, "default": 25},
        ],
    },
    "bracket": {
        "label": "Bracket plate",
        "blurb": "Flat plate with two holes.",
        "params": [
            {"key": "L", "label": "Length", "unit": "mm", "ptype": "number", "min": 10, "max": 300, "step": 0.5, "default": 60},
            {"key": "W", "label": "Width", "unit": "mm", "ptype": "number", "min": 10, "max": 200, "step": 0.5, "default": 30},
            {"key": "T", "label": "Thickness", "unit": "mm", "ptype": "number", "min": 1, "max": 50, "step": 0.5, "default": 5},
            {"key": "D", "label": "Hole dia", "unit": "mm", "ptype": "number", "min": 1, "max": 50, "step": 0.5, "default": 5},
        ],
    },
    "gridfinity_bin": {
        "label": "Gridfinity Bin",
        "blurb": "Spec-based storage bin: 42mm grid, stacking foot, lip + magnets optional.",
        "params": [
            {"key": "GX", "label": "Grid X", "unit": "u", "ptype": "int", "min": 1, "max": 6, "step": 1, "default": 2},
            {"key": "GY", "label": "Grid Y", "unit": "u", "ptype": "int", "min": 1, "max": 6, "step": 1, "default": 2},
            {"key": "HU", "label": "Height", "unit": "u", "ptype": "int", "min": 1, "max": 12, "step": 1, "default": 6},
            {"key": "WALL", "label": "Wall", "unit": "mm", "ptype": "number", "min": 0.8, "max": 2.4, "step": 0.2, "default": 1.2},
            {"key": "MAGNETS", "label": "Magnet holes (6x2)", "ptype": "bool", "default": True},
            {"key": "LIP", "label": "Stacking lip", "ptype": "bool", "default": True},
        ],
    },
    "gridfinity_baseplate": {
        "label": "Gridfinity Baseplate",
        "blurb": "Grid the bins snap into, with sockets + magnet holes.",
        "params": [
            {"key": "GX", "label": "Grid X", "unit": "u", "ptype": "int", "min": 1, "max": 6, "step": 1, "default": 4},
            {"key": "GY", "label": "Grid Y", "unit": "u", "ptype": "int", "min": 1, "max": 6, "step": 1, "default": 4},
            {"key": "T", "label": "Thickness", "unit": "mm", "ptype": "number", "min": 4.6, "max": 8, "step": 0.2, "default": 5},
            {"key": "SOCKETS", "label": "Bin sockets", "ptype": "bool", "default": True},
            {"key": "MAGNETS", "label": "Magnet holes (6x2)", "ptype": "bool", "default": True},
        ],
    },
}


def _num(v, name):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {v!r}")
    if not (f == f and abs(f) != float("inf")):
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
            cleaned[key] = int(v)
        else:
            cleaned[key] = v
    if primitive == "tube" and not cleaned["R_IN"] < cleaned["R_OUT"]:
        raise ValueError("R_IN must be smaller than R_OUT")
    if primitive == "bracket":
        if cleaned["D"] >= min(cleaned["L"] / 2, cleaned["W"]):
            raise ValueError("Hole diameter D too large for plate size")
    if primitive == "gridfinity_bin":
        if 2 * cleaned["WALL"] >= min(cleaned["GX"], cleaned["GY"]) * GF_PITCH - GF_TOL - 4:
            raise ValueError("WALL too thick for this grid size")
    if primitive == "gridfinity_baseplate":
        if cleaned["MAGNETS"] and cleaned["T"] < 4.6:
            raise ValueError("Thickness >= 4.6mm needed for magnet holes")
    return cleaned


def _gridfinity_bin_code(c):
    gx, gy, hu = c["GX"], c["GY"], c["HU"]
    lines = [
        "from build123d import *",
        f"GX, GY, HU = {gx}, {gy}, {hu}",
        f"WALL = {c['WALL']}",
        f"BASE_H = {GF_BASE_H}",
        f"W = GX * {GF_PITCH} - {GF_TOL}",
        f"D = GY * {GF_PITCH} - {GF_TOL}",
        f"H = HU * {GF_HU}",
        f"_outer = extrude(Plane.XY * RectangleRounded(W, D, {GF_CORNER}), amount=H)",
        "_cw = W - 2 * WALL",
        "_cd = D - 2 * WALL",
        f"_cavity = Pos(0, 0, BASE_H) * extrude(Plane.XY * RectangleRounded(_cw, _cd, max(0.5, {GF_CORNER} - WALL)), amount=H - BASE_H + 1)",
        "result = _outer - _cavity",
        "# stacking feet, one per cell (simplified stepped spec profile)",
        "_widths = (35.6, 37.2, 41.5)",
        "_heights = (0.8, 1.8, 2.15)",
        "_z = 0.0",
        "_parts = []",
        "for _w, _h in zip(_widths, _heights):",
        "    for _ix in range(GX):",
        "        for _iy in range(GY):",
        f"            _cx = -(GX * {GF_PITCH} - {GF_TOL}) / 2 + {GF_PITCH / 2} + _ix * {GF_PITCH}",
        f"            _cy = -(GY * {GF_PITCH} - {GF_TOL}) / 2 + {GF_PITCH / 2} + _iy * {GF_PITCH}",
        "            _parts.append(Pos(_cx, _cy, _z) * Box(_w, _w, _h, align=(Align.CENTER, Align.CENTER, Align.MIN)))",
        "    _z += _h",
        "_feet = _parts[0]",
        "for _p in _parts[1:]:",
        "    _feet += _p",
        "result += _feet",
    ]
    if c["MAGNETS"]:
        lines += [
            f"# magnet holes (6x2mm magnets, 26mm grid per cell)",
            "for _ix in range(GX):",
            "    for _iy in range(GY):",
            f"        _cx = -(GX * {GF_PITCH} - {GF_TOL}) / 2 + {GF_PITCH / 2} + _ix * {GF_PITCH}",
            f"        _cy = -(GY * {GF_PITCH} - {GF_TOL}) / 2 + {GF_PITCH / 2} + _iy * {GF_PITCH}",
            f"        for _dx in (-{GF_MAG_D}, {GF_MAG_D}):",
            f"            for _dy in (-{GF_MAG_D}, {GF_MAG_D}):",
            f"                result -= Pos(_cx + _dx, _cy + _dy, -0.5) * Cylinder({GF_MAG_R}, 3.1, align=(Align.CENTER, Align.CENTER, Align.MIN))",
        ]
    if c["LIP"]:
        lines += [
            "# stacking lip: rim frame above the walls",
            "_lw = W - 2.0",
            "_ld = D - 2.0",
            "_lip = extrude(Plane.XY * RectangleRounded(_lw, _ld, 3.0), amount=4.4) - extrude(Plane.XY * RectangleRounded(_lw - 2 * WALL, _ld - 2 * WALL, 2.0), amount=5.4)",
            "result += Pos(0, 0, H) * _lip",
        ]
    return "\n".join(lines) + "\n"


def _gridfinity_baseplate_code(c):
    gx, gy = c["GX"], c["GY"]
    lines = [
        "from build123d import *",
        f"GX, GY = {gx}, {gy}",
        f"T = {c['T']}",
        f"W = GX * {GF_PITCH}",
        f"D = GY * {GF_PITCH}",
        "result = extrude(Plane.XY * RectangleRounded(W, D, 2.0), amount=T)",
        "for _ix in range(GX):",
        "    for _iy in range(GY):",
        f"        _cx = -W / 2 + {GF_PITCH / 2} + _ix * {GF_PITCH}",
        f"        _cy = -D / 2 + {GF_PITCH / 2} + _iy * {GF_PITCH}",
    ]
    if c["SOCKETS"]:
        lines += [
            "        _sock = Pos(_cx, _cy, T - 2.0) * extrude(Plane.XY * RectangleRounded(40.0, 40.0, 3.0), amount=3.0)",
            "        result -= _sock",
        ]
    if c["MAGNETS"]:
        lines += [
            f"        result -= Pos(_cx, _cy, T - 4.6) * Cylinder({GF_MAG_R}, 2.8, align=(Align.CENTER, Align.CENTER, Align.MIN))",
        ]
    return "\n".join(lines) + "\n"


def generate_primitive_code(primitive, params):
    """Return build123d algebra-mode code string assigning `result`."""
    c = validate_primitive_params(primitive, params)
    if primitive == "box":
        return f"from build123d import *\nresult = Box({c['L']}, {c['W']}, {c['H']})\n"
    if primitive == "cylinder":
        return f"from build123d import *\nresult = Cylinder({c['R']}, {c['H']})\n"
    if primitive == "tube":
        return (
            "from build123d import *\n"
            f"result = Cylinder({c['R_OUT']}, {c['H']}) - Cylinder({c['R_IN']}, {c['H']} + 2)\n"
        )
    if primitive == "bracket":
        return (
            "from build123d import *\n"
            f"L, W, T, D = {c['L']}, {c['W']}, {c['T']}, {c['D']}\n"
            "plate = Box(L, W, T)\n"
            "hole = Cylinder(D / 2, T + 2)\n"
            "h1 = Pos(-L / 4, 0, -1) * hole\n"
            "h2 = Pos(L / 4, 0, -1) * hole\n"
            "result = plate - h1 - h2\n"
        )
    if primitive == "gridfinity_bin":
        return _gridfinity_bin_code(c)
    if primitive == "gridfinity_baseplate":
        return _gridfinity_baseplate_code(c)
    raise ValueError(f"unknown object {primitive!r}")


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
        try:
            if isinstance(obj, Shape):
                return obj, key
        except Exception:
            continue
    return None, None


def _shape_stats(shape):
    stats = {}
    for attr in ("volume", "area"):
        try:
            v = getattr(shape, attr, None)
            stats[attr + "_mm"] = round(float(v() if callable(v) else v), 3)
        except Exception:
            pass
    try:
        bb = shape.bounding_box()
        stats["bbox_min"] = [round(float(x), 3) for x in bb.min]
        stats["bbox_max"] = [round(float(x), 3) for x in bb.max]
        stats["bbox_size"] = [round(float(x), 3) for x in bb.size]
    except Exception:
        pass
    return stats


def _preview_payload(shape, tolerance=DEFAULT_TOLERANCE):
    """Decimated triangle soup for the page's 3D preview. Best-effort: None on failure.

    Returns {"tris": [x1,y1,z1, ...], "shown": n, "total": m} or None.
    """
    try:
        vertices, triangles = shape.tessellate(float(tolerance), 0.1)
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


def run_build123d_code(code, export_format="stl", tolerance=DEFAULT_TOLERANCE,
                       filename_stem="model"):
    """Execute user code, export result. Returns dict (JSON-able). No orca needed.

    Raises RuntimeError with user-facing message on failure.
    """
    if export_format not in EXPORT_FORMATS:
        raise ValueError(f"format must be one of {EXPORT_FORMATS}")
    tol = float(tolerance)
    if not (0.0001 <= tol <= 1.0):
        raise ValueError("tolerance must be within [0.0001, 1.0]")
    if len(code) > 200_000:
        raise ValueError("code too large (>200k chars)")

    try:
        import build123d  # noqa: F401  (ensures dependency present)
    except Exception as exc:
        raise RuntimeError(
            "build123d is not installed in Orca's Python environment yet. "
            "Reopen Plugins dialog / restart OrcaSlicer so bundled `uv` can "
            f"install dependencies, then retry. ({exc})"
        )

    namespace = {"__name__": "__orcad__"}
    try:
        exec("from build123d import *", namespace)  # noqa: S102 - intentional CAD exec
        exec(compile(code, "<orcad>", "exec"), namespace)  # noqa: S102
    except Exception as exc:
        tb = traceback.format_exc(limit=8)
        raise RuntimeError(f"CAD code failed: {exc}\n{tb}")

    shape, var = _find_result(namespace)
    if shape is None:
        raise RuntimeError(
            "No result found. Assign your final solid to variable `result` "
            "(e.g. `result = Box(20, 20, 20)`)."
        )
    # volume sanity (2D sketches have ~0 volume -> STL would be empty)
    try:
        vol = float(shape.volume() if callable(getattr(shape, "volume", None))
                    else shape.volume)
        if vol <= 0:
            raise RuntimeError(
                f"`{var}` has zero volume ({vol}). STL/3MF need a solid — "
                "did you build a flat sketch? Extrude it first."
            )
    except RuntimeError:
        raise
    except Exception:
        pass  # best-effort only; let exporter decide

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
        raise RuntimeError(f"Export to {export_format} failed: {exc}")

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
        "size_bytes": int(size),
        "stats": _shape_stats(shape),
        "preview": _preview_payload(shape, tol),
    }


# ---------------------------------------------------------------------------
# Page UI — fully self-contained (inline CSS, no framework CDN).
# Orca's WebView does not reliably load external stylesheets, so the modern
# theme ships in the page and adapts via --orca-* vars (dark-first fallback).
# Only Monaco loads from CDN, with an automatic textarea fallback.
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>orcad</title>
<style>
:root{
  color-scheme:light dark;
  --bg:var(--orca-bg,#14161b); --bg2:var(--orca-bg,#1b1e25);
  --fg:var(--orca-fg,#e9ebef); --muted:var(--orca-muted,#9aa1ad);
  --border:var(--orca-border,#2c313b); --accent:var(--orca-accent,#22b8a8);
  --accent-fg:var(--orca-accent-fg,#062a27); --danger:#e5484d;
  --ui:var(--orca-font,system-ui,-apple-system,'Segoe UI',Roboto,Inter,sans-serif);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --r:10px;
}
@media (prefers-color-scheme:light){
  :root{--bg:var(--orca-bg,#f6f7f9);--bg2:var(--orca-bg,#ffffff);
    --fg:var(--orca-fg,#1c2026);--muted:var(--orca-muted,#66707d);
    --border:var(--orca-border,#e0e4ea);--accent:var(--orca-accent,#0b8067);--accent-fg:var(--orca-accent-fg,#fff)}
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13.5px/1.55 var(--ui);-webkit-font-smoothing:antialiased}
.mono{font-family:var(--mono)} .muted{color:var(--muted)} .small{font-size:12px}
/* header */
.top{position:sticky;top:0;z-index:20;display:flex;align-items:center;gap:10px;flex-wrap:wrap;
  padding:10px 16px;background:var(--bg2);border-bottom:1px solid var(--border)}
.brand{font-size:15px;font-weight:700;letter-spacing:.01em}
.brand small{color:var(--muted);font-weight:400}
.spacer{flex:1}
select,input[type=number],input[type=text],textarea{background:var(--bg);color:var(--fg);
  border:1px solid var(--border);border-radius:8px;padding:6px 10px;font:inherit}
select:focus-visible,input:focus-visible,textarea:focus-visible,button:focus-visible{outline:2px solid var(--accent);outline-offset:1px}
.btn{font:inherit;font-weight:600;border-radius:8px;padding:7px 14px;cursor:pointer;border:1px solid transparent}
.btn-p{background:var(--accent);color:var(--accent-fg)}
.btn-p:hover{filter:brightness(1.08)} .btn-p:disabled{opacity:.55;cursor:default}
.btn-g{background:transparent;color:var(--fg);border-color:var(--border)}
.btn-g:hover{border-color:var(--accent)} .btn-s{padding:4px 10px;font-size:12px;border-radius:7px}
/* layout */
.wrap{padding:14px 16px 24px;max-width:1500px;margin:0 auto}
.grid{display:grid;grid-template-columns:360px minmax(0,1fr);gap:14px;align-items:start}
@media(max-width:900px){.grid{grid-template-columns:1fr}}
.card{background:var(--bg2);border:1px solid var(--border);border-radius:var(--r);padding:14px;margin-bottom:14px}
.card h5{margin:0 0 4px;font-size:13px} .card h3{margin:0 0 10px;font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.sec-t{font-size:11px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 8px}
/* tabs */
.tabs{display:flex;gap:4px;background:var(--bg);border:1px solid var(--border);border-radius:10px;padding:4px;margin-bottom:12px}
.tabs button{flex:1;border:0;background:transparent;color:var(--muted);font:inherit;font-weight:600;
  padding:8px;border-radius:7px;cursor:pointer}
.tabs button.on{background:var(--accent);color:var(--accent-fg)}
/* dropdown */
.dd{position:relative}
.dd-btn{width:100%;display:flex;align-items:center;gap:8px;text-align:left;background:var(--bg);
  border:1px solid var(--border);border-radius:9px;padding:9px 12px;color:var(--fg);font:inherit;cursor:pointer}
.dd-btn:hover{border-color:var(--accent)}
.dd-btn .caret{margin-left:auto;color:var(--muted)}
.dd-list{position:absolute;top:calc(100% + 6px);left:0;right:0;z-index:30;background:var(--bg2);
  border:1px solid var(--border);border-radius:10px;overflow:hidden;box-shadow:0 12px 32px rgba(0,0,0,.35)}
.dd-list input{width:100%;border:0;border-bottom:1px solid var(--border);border-radius:0}
.dd-opts{max-height:240px;overflow:auto;padding:4px}
.dd-opts button{display:block;width:100%;text-align:left;border:0;background:transparent;color:var(--fg);
  font:inherit;padding:8px 10px;border-radius:7px;cursor:pointer}
.dd-opts button small{display:block;color:var(--muted);font-size:11.5px}
.dd-opts button:hover,.dd-opts button.hot{background:var(--accent);color:var(--accent-fg)}
.dd-opts button:hover small,.dd-opts button.hot small{color:inherit;opacity:.8}
.obj-blurb{font-size:12px;color:var(--muted);margin:8px 2px 0}
/* params */
.prow{display:grid;grid-template-columns:1fr auto;gap:2px 10px;align-items:center;
  padding:9px 2px;border-bottom:1px dashed var(--border)}
.prow:last-of-type{border-bottom:0}
.prow label{font-size:12.5px} .prow label b{font-weight:650}
.prow .u{color:var(--muted);font-size:11px;margin-left:4px}
.prow input[type=number]{width:84px;text-align:right;padding:5px 8px}
.prow input[type=range]{grid-column:1/-1;width:100%;margin:2px 0 0}
.prow input[type=checkbox]{width:20px;height:20px;accent-color:var(--accent);justify-self:end}
input[type=range]{-webkit-appearance:none;appearance:none;height:22px;background:transparent;cursor:pointer}
input[type=range]::-webkit-slider-runnable-track{height:6px;border-radius:3px;background:var(--border)}
input[type=range]::-webkit-slider-thumb{-webkit-appearance:none;width:16px;height:16px;margin-top:-5px;
  border-radius:50%;background:var(--accent);border:2px solid var(--bg2)}
input[type=range]::-moz-range-track{height:6px;border-radius:3px;background:var(--border)}
input[type=range]::-moz-range-thumb{width:12px;height:12px;border-radius:50%;background:var(--accent);border:2px solid var(--bg2)}
/* preview / result / log */
#pv3d{width:100%;height:380px;border:1px solid var(--border);border-radius:8px;cursor:grab;touch-action:none;background:var(--bg)}
.pvbar{display:flex;align-items:center;gap:8px;flex-wrap:wrap;margin-bottom:8px}
.pvbar h5{margin:0;font-size:14px}
.kv{display:grid;grid-template-columns:150px 1fr;gap:3px 10px;font-size:12.5px}
.kv .k{color:var(--muted)} .kv .v{font-family:var(--mono);word-break:break-word}
.log{font-family:var(--mono);font-size:12px;max-height:190px;overflow:auto;white-space:pre-wrap;
  background:var(--bg);border:1px solid var(--border);border-radius:8px;padding:9px 11px}
.alert-err{background:rgba(229,72,77,.12);border:1px solid var(--danger);border-radius:8px;padding:9px 11px;font-family:var(--mono);font-size:12px}
#editor{height:340px;border:1px solid var(--border);border-radius:8px;overflow:hidden}
#code{width:100%;min-height:340px;font-family:var(--mono);font-size:12px}
.rowline{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:8px 0}
.banner{border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:8px;padding:9px 12px;margin-bottom:12px;font-size:12.5px}
</style>
</head>
<body>
<header class="top">
  <span class="brand">orcad <small>build123d · v0.3</small></span>
  <span id="status" class="muted small"></span><span class="spacer"></span>
  <select id="fmt" title="Export format"><option value="stl">STL</option><option value="step">STEP</option><option value="3mf">3MF</option></select>
  <input id="tol" type="number" value="0.001" step="0.001" min="0.0001" max="1" style="width:86px" title="Tessellation tolerance">
  <button id="runBtn" class="btn btn-p" onclick="runActive()">Run + Export</button>
</header>

<div class="wrap"><div class="grid">
  <div>
    <div class="tabs" role="tablist">
      <button id="tabbtn-objs" class="on" onclick="switchLeft('objs')">Objects</button>
      <button id="tabbtn-editor" onclick="switchLeft('editor')">Code Editor</button>
    </div>
    <div id="pane-objs">
      <div class="card"><h3>Predefined object</h3>
        <div class="dd">
          <button class="dd-btn" id="ddBtn" onclick="ddToggle(event)"><span id="ddLabel">Gridfinity Bin</span><span class="caret">▾</span></button>
          <div class="dd-list" id="ddList" style="display:none">
            <input id="objSearch" type="text" placeholder="Search objects…" oninput="ddFilter()" autocomplete="off">
            <div class="dd-opts" id="objList"></div>
          </div>
        </div>
        <div class="obj-blurb" id="objBlurb"></div>
      </div>
      <div class="card"><h3>Parameters</h3><div id="prims"></div>
        <button class="btn btn-p" style="width:100%;margin-top:10px" onclick="generate()">Generate + Export</button>
      </div>
    </div>
    <div id="pane-editor" style="display:none">
      <div class="card"><h3>build123d · algebra mode</h3>
        <div class="banner">Assign the final solid to <span class="mono">result</span> — e.g. <span class="mono">result = Box(20,20,20)</span>.</div>
        <div id="editor"></div>
        <textarea id="code" style="display:none" spellcheck="false"></textarea>
        <div class="rowline">
          <input id="fname" value="model" style="width:140px" title="File name">
          <select id="exSel" style="flex:1;min-width:140px"></select>
          <button class="btn btn-g btn-s" onclick="loadExample()">Load</button>
        </div>
      </div>
    </div>
    <div class="card"><h3>Manual import</h3>
      <div class="small muted">Plugins can't push models into the plater yet — drag the exported file onto Prepare to slice it.</div>
    </div>
  </div>

  <div>
    <div class="card">
      <div class="pvbar"><h5>Preview</h5><span id="pvInfo" class="muted small">nothing rendered yet — run a model</span><span class="spacer"></span>
        <button class="btn btn-g btn-s" onclick="pvReset()">Reset view</button>
        <button class="btn btn-g btn-s" id="wireBtn" onclick="pvToggleWire()">Wireframe: off</button>
        <button class="btn btn-g btn-s" id="spinBtn" onclick="pvToggleSpin()">Spin: on</button>
      </div>
      <canvas id="pv3d"></canvas>
      <div id="pvStats" class="mono small muted" style="margin-top:8px">—</div>
    </div>
    <div class="card"><h5>Result</h5><div id="result" class="muted">Nothing exported yet.</div></div>
    <div class="card"><h5>Log</h5><div id="log" class="log">ready.
</div></div>
  </div>
</div></div>

<script src="https://cdn.jsdelivr.net/npm/monaco-editor@0.49.0/min/vs/loader.js"></script>
<script>
'use strict';
/* ---------------- state ----------------
   PRIMS mirrors the Python PRIMITIVES spec (keys + labels + param types).
   A test enforces key parity; values here drive the dropdown + sliders. */
var S={left:'objs',prim:'gridfinity_bin',params:{}};
var PRIMS={
 box:{label:'Box',blurb:'Simple centered block.',params:[['L','Length','mm','number',20,1,300,0.5],['W','Width','mm','number',20,1,300,0.5],['H','Height','mm','number',20,1,300,0.5]]},
 cylinder:{label:'Cylinder',blurb:'Round post or puck.',params:[['R','Radius','mm','number',10,0.5,150,0.5],['H','Height','mm','number',20,1,300,0.5]]},
 tube:{label:'Tube',blurb:'Hollow cylinder.',params:[['R_OUT','Outer radius','mm','number',12,1,150,0.5],['R_IN','Inner radius','mm','number',8,0.5,149,0.5],['H','Height','mm','number',25,1,300,0.5]]},
 bracket:{label:'Bracket plate',blurb:'Flat plate with two holes.',params:[['L','Length','mm','number',60,10,300,0.5],['W','Width','mm','number',30,10,200,0.5],['T','Thickness','mm','number',5,1,50,0.5],['D','Hole dia','mm','number',5,1,50,0.5]]},
 gridfinity_bin:{label:'Gridfinity Bin',blurb:'Spec-based bin: 42mm grid, stacking foot, lip + magnets optional.',params:[['GX','Grid X','u','int',2,1,6,1],['GY','Grid Y','u','int',2,1,6,1],['HU','Height','u','int',6,1,12,1],['WALL','Wall','mm','number',1.2,0.8,2.4,0.2],['MAGNETS','Magnet holes (6x2)','','bool',1],['LIP','Stacking lip','','bool',1]]},
 gridfinity_baseplate:{label:'Gridfinity Baseplate',blurb:'Grid the bins snap into, with sockets + magnet holes.',params:[['GX','Grid X','u','int',4,1,6,1],['GY','Grid Y','u','int',4,1,6,1],['T','Thickness','mm','number',5,4.6,8,0.2],['SOCKETS','Bin sockets','','bool',1],['MAGNETS','Magnet holes (6x2)','','bool',1]]}
};
var EXAMPLES={
 calibration_cube:'from build123d import *\nresult = Box(20, 20, 20)\n',
 bracket:'from build123d import *\nL, W, T, D = 60, 30, 5, 5\nplate = Box(L, W, T)\nhole = Cylinder(D / 2, T + 2)\nh1 = Pos(-L / 4, 0, -1) * hole\nh2 = Pos(L / 4, 0, -1) * hole\nresult = plate - h1 - h2\n',
 tube_demo:'from build123d import *\nouter = Cylinder(12, 25)\ninner = Cylinder(8, 27)\ntube = outer - inner\nring = Pos(0, 0, 25) * Cylinder(10, 3)\nresult = tube + ring\n',
 gridfinity_bin_2x2x6:'(generated — pick Gridfinity Bin in Objects and press Generate, or run any snippet that sets `result`)'
};
/* ---------------- helpers ---------------- */
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function log(m){var el=document.getElementById('log');el.textContent+=m+'\n';el.scrollTop=el.scrollHeight;}
function setStatus(m){document.getElementById('status').textContent=m;}
function send(o){try{window.orca.postMessage(o);}catch(e){log('bridge error: '+e);}}
function fmt(){return document.getElementById('fmt').value;}
function tol(){return parseFloat(document.getElementById('tol').value)||0.001;}
/* ---------------- left tabs ---------------- */
function switchLeft(which){
 S.left=which;
 document.getElementById('pane-objs').style.display=which==='objs'?'':'none';
 document.getElementById('pane-editor').style.display=which==='editor'?'':'none';
 document.getElementById('tabbtn-objs').classList.toggle('on',which==='objs');
 document.getElementById('tabbtn-editor').classList.toggle('on',which==='editor');
 if(which==='editor')setTimeout(monacoLayout,30);
}
function runActive(){ if(S.left==='objs')generate(); else runEditor(); }
/* ---------------- searchable objects dropdown ---------------- */
function ddToggle(ev){ev.stopPropagation();var l=document.getElementById('ddList');
 var open=l.style.display!=='none';l.style.display=open?'none':'';
 if(!open){document.getElementById('objSearch').value='';ddFilter();setTimeout(function(){document.getElementById('objSearch').focus();},20);}}
document.addEventListener('click',function(e){
 var l=document.getElementById('ddList');
 if(l.style.display!=='none'&&!document.getElementById('ddBtn').contains(e.target)&&!l.contains(e.target))l.style.display='none';});
function ddFilter(){
 var q=document.getElementById('objSearch').value.trim().toLowerCase();
 var host=document.getElementById('objList');host.innerHTML='';
 Object.keys(PRIMS).forEach(function(k){
  var p=PRIMS[k];
  if(q&&p.label.toLowerCase().indexOf(q)<0&&k.indexOf(q)<0)return;
  var b=document.createElement('button');
  b.innerHTML=esc(p.label)+'<small>'+esc(p.blurb||'')+'</small>';
  if(k===S.prim)b.classList.add('hot');
  b.onclick=function(){S.prim=k;S.params={};document.getElementById('ddList').style.display='none';buildObjs();};
  host.appendChild(b);
 });
 if(!host.children.length)host.innerHTML='<div class="muted small" style="padding:8px 10px">No objects match.</div>';
}
function buildObjs(){
 var p=PRIMS[S.prim];
 document.getElementById('ddLabel').textContent=p.label;
 document.getElementById('objBlurb').textContent=p.blurb||'';
 ddFilter();
 var host=document.getElementById('prims');host.innerHTML='';
 p.params.forEach(function(spec){
  var key=spec[0],lab=spec[1],unit=spec[2],type=spec[3],def=spec[4],mn=spec[5],mx=spec[6],st=spec[7];
  var row=document.createElement('div');row.className='prow';
  if(type==='bool'){
   var val=(S.params[key]!=null)?!!S.params[key]:!!def;S.params[key]=val;
   row.innerHTML='<label><b>'+esc(key)+'</b> '+esc(lab)+'</label>';
   var cb=document.createElement('input');cb.type='checkbox';cb.checked=val;
   cb.onchange=function(){S.params[key]=cb.checked;};
   row.appendChild(cb);host.appendChild(row);return;
  }
  var val=(S.params[key]!=null)?S.params[key]:def;S.params[key]=val;
  row.innerHTML='<label><b>'+esc(key)+'</b> '+esc(lab)+'<span class="u">'+esc(unit)+'</span></label>';
  var num=document.createElement('input');num.type='number';num.value=val;num.step=st;num.min=mn;num.max=mx;
  if(type==='int'){num.oninput=function(){S.params[key]=parseInt(num.value,10);rng.value=num.value;};}
  else{num.oninput=function(){S.params[key]=parseFloat(num.value);rng.value=num.value;};}
  row.appendChild(num);
  var rng=document.createElement('input');rng.type='range';rng.min=mn;rng.max=mx;rng.step=st;rng.value=val;
  if(type==='int'){rng.oninput=function(){S.params[key]=parseInt(rng.value,10);num.value=rng.value;};}
  else{rng.oninput=function(){S.params[key]=parseFloat(rng.value);num.value=rng.value;};}
  row.appendChild(rng);host.appendChild(row);
 });
}
/* ---------------- generate ---------------- */
function generate(){
 setStatus('working…');log('object '+S.prim+' '+JSON.stringify(S.params));
 send({command:'generate',primitive:S.prim,params:S.params,format:fmt(),tolerance:tol(),filename:S.prim});
}
/* ---------------- editor (Monaco w/ textarea fallback) ---------------- */
var monacoInst=null, monacoReady=false;
function getCode(){return monacoReady&&monacoInst?monacoInst.getValue():document.getElementById('code').value;}
function setCode(v){if(monacoReady&&monacoInst)monacoInst.setValue(v);document.getElementById('code').value=v;}
function monacoLayout(){try{if(monacoReady&&monacoInst)monacoInst.layout();}catch(e){}}
function monacoFallback(reason){
 if(monacoReady)return;monacoReady=false;
 document.getElementById('editor').style.display='none';
 document.getElementById('code').style.display='';
 if(!document.getElementById('code').value)document.getElementById('code').value=EXAMPLES.calibration_cube;
 log('editor: plain textarea fallback ('+reason+')');
}
function initMonaco(){
 var ta=document.getElementById('code');
 if(!window.require||!window.require.config){monacoFallback('loader blocked/offline');return;}
 var timer=setTimeout(function(){if(!monacoReady)monacoFallback('load timeout (offline?)');},8000);
 try{
  window.require.config({paths:{vs:'https://cdn.jsdelivr.net/npm/monaco-editor@0.49.0/min/vs'}});
  window.require(['vs/editor/editor.main'],function(){
   clearTimeout(timer);
   var dark=window.matchMedia&&window.matchMedia('(prefers-color-scheme: dark)').matches;
   monacoInst=window.monaco.editor.create(document.getElementById('editor'),{
    value:ta.value||EXAMPLES.calibration_cube,language:'python',
    theme:dark?'vs-dark':'vs',automaticLayout:true,minimap:{enabled:false},fontSize:13,scrollBeyondLastLine:false});
   monacoReady=true;ta.style.display='none';log('editor: monaco ready');
  },function(){clearTimeout(timer);monacoFallback('module load failed');});
 }catch(e){clearTimeout(timer);monacoFallback('init error');}
}
function runEditor(){
 var code=getCode();
 var fn=document.getElementById('fname').value||'model';
 setStatus('working…');log('run '+code.length+' chars -> '+fn+'.'+fmt());
 send({command:'run',code:code,format:fmt(),tolerance:tol(),filename:fn});
}
function loadExample(){var k=document.getElementById('exSel').value;setCode(EXAMPLES[k]);log('loaded '+k);}
/* ---------------- 3D preview (dependency-free canvas) ---------------- */
var PV={tris:[],total:0,yaw:0.7,pitch:0.55,zoom:1,wire:false,spin:true,ext:null};
function pvCss(v,f){try{var s=getComputedStyle(document.body).getPropertyValue(v);if(s&&s.trim())return s.trim();}catch(e){}return f;}
function pvFit(){
 var t=PV.tris;if(!t.length){PV.ext=null;return;}
 var mnx=1/0,mxx=-1/0,mny=1/0,mxy=-1/0,mnz=1/0,mxz=-1/0;
 for(var i=0;i<t.length;i+=3){var x=t[i],y=t[i+1],z=t[i+2];
  if(x<mnx)mnx=x;if(x>mxx)mxx=x;if(y<mny)mny=y;if(y>mxy)mxy=y;if(z<mnz)mnz=z;if(z>mxz)mxz=z;}
 PV.ext={c:[(mnx+mxx)/2,(mny+mxy)/2,(mnz+mxz)/2],d:Math.max(mxx-mnx,mxy-mny,mxz-mnz,1e-6)};
}
function pvSet(p){
 PV.tris=(p&&p.tris)||[];PV.total=(p&&p.total)||0;pvFit();pvDraw();
 document.getElementById('pvInfo').textContent=PV.tris.length?('mesh: '+PV.total+' tris'+(PV.total>PV.tris.length/9?' (decimated preview)':'')):'preview unavailable — stats only';
}
function pvDraw(){
 var cv=document.getElementById('pv3d');if(!cv||!cv.clientWidth)return;
 var dpr=window.devicePixelRatio||1,W=cv.clientWidth,H=cv.clientHeight;
 if(cv.width!==W*dpr||cv.height!==H*dpr){cv.width=W*dpr;cv.height=H*dpr;}
 var ctx=cv.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
 var t=PV.tris;
 ctx.fillStyle=pvCss('--muted','#9aa1ad');ctx.font='12px sans-serif';
 if(!t.length||!PV.ext){ctx.fillText('Run a model to see the 3D preview here.',14,H/2);return;}
 var cy=Math.cos(PV.yaw),sy=Math.sin(PV.yaw),cp=Math.cos(PV.pitch),sp=Math.sin(PV.pitch);
 var sc=Math.min(W,H)*0.38/PV.ext.d*PV.zoom,cx=W/2,cy0=H/2;
 var n=t.length/9,order=new Array(n),i,j;
 for(i=0;i<n;i++)order[i]=i;
 var P=new Float64Array(t.length);
 for(i=0;i<t.length;i+=3){
  var x=t[i]-PV.ext.c[0],y=t[i+1]-PV.ext.c[1],z=t[i+2]-PV.ext.c[2];
  var x1=x*cy-y*sy,y1=x*sy+y*cy;
  var y2=y1*cp-z*sp,z2=y1*sp+z*cp;
  P[i]=cx+x1*sc;P[i+1]=cy0-y2*sc;P[i+2]=z2;
 }
 order.sort(function(a,b){
  var za=(P[a*9+2]+P[a*9+5]+P[a*9+8])/3,zb=(P[b*9+2]+P[b*9+5]+P[b*9+8])/3;
  return za-zb;});
 var lx=0.35,ly=0.5,lz=0.79;
 function shade(nx,ny,nz){var d=nx*lx+ny*ly+nz*lz;if(d<0)d=0;
  var k=0.35+0.65*d;return 'rgb('+Math.round(120*k+40)+','+Math.round(150*k+40)+','+Math.round(220*k+30)+')';}
 for(j=0;j<n;j++){
  i=order[j]*9;
  var ax=P[i],ay=P[i+1],bx=P[i+3],by=P[i+4],cx2=P[i+6],cy2=P[i+7];
  var ux=bx-ax,uy=by-ay,uz=P[i+5]-P[i+2],vx=cx2-ax,vy=cy2-ay,vz=P[i+8]-P[i+2];
  var nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx;
  var nl=Math.sqrt(nx*nx+ny*ny+nz*nz)||1;nx/=nl;ny/=nl;nz/=nl;
  ctx.beginPath();ctx.moveTo(ax,ay);ctx.lineTo(bx,by);ctx.lineTo(cx2,cy2);ctx.closePath();
  if(PV.wire){ctx.strokeStyle=pvCss('--accent','#22b8a8');ctx.lineWidth=0.7;ctx.stroke();}
  else{ctx.fillStyle=shade(nx,ny,nz);ctx.fill();ctx.strokeStyle='rgba(0,0,0,0.12)';ctx.lineWidth=0.4;ctx.stroke();}
 }
}
function pvReset(){PV.yaw=0.7;PV.pitch=0.55;PV.zoom=1;pvDraw();}
function pvToggleWire(){PV.wire=!PV.wire;document.getElementById('wireBtn').textContent='Wireframe: '+(PV.wire?'on':'off');pvDraw();}
function pvToggleSpin(){PV.spin=!PV.spin;document.getElementById('spinBtn').textContent='Spin: '+(PV.spin?'on':'off');}
(function(){
 var cv=document.getElementById('pv3d'),drag=null;
 cv.addEventListener('pointerdown',function(e){drag={x:e.clientX,y:e.clientY};try{cv.setPointerCapture(e.pointerId);}catch(_){}cv.style.cursor='grabbing';});
 cv.addEventListener('pointermove',function(e){if(!drag)return;PV.yaw+=(e.clientX-drag.x)*0.008;PV.pitch+=(e.clientY-drag.y)*0.008;drag={x:e.clientX,y:e.clientY};pvDraw();});
 ['pointerup','pointercancel','pointerleave'].forEach(function(ev){cv.addEventListener(ev,function(){drag=null;cv.style.cursor='grab';});});
 cv.addEventListener('wheel',function(e){e.preventDefault();PV.zoom*=e.deltaY>0?0.92:1.08;PV.zoom=Math.min(8,Math.max(0.2,PV.zoom));pvDraw();},{passive:false});
 cv.addEventListener('dblclick',pvReset);
 window.addEventListener('resize',pvDraw);
 setInterval(function(){if(PV.spin&&PV.tris.length){PV.yaw+=0.025;pvDraw();}},80);
})();
/* ---------------- bridge ---------------- */
function showResult(d){
 var el=document.getElementById('result');
 document.getElementById('pvStats').textContent='stats: '+JSON.stringify(d.stats||{});
 if(d.ok){
  el.innerHTML='<div class="kv">'
   +'<div class="k">file</div><div class="v">'+esc(d.file)+'</div>'
   +'<div class="k">size</div><div class="v">'+esc(d.size_bytes)+' bytes</div>'
   +'<div class="k">solid</div><div class="v">'+esc(d.var||'result')+'</div></div>'
   +'<p class="muted small">Drag this file onto Prepare to slice it.</p>';
  log('OK '+d.filename+' ('+d.size_bytes+' B)');
 }else{
  el.innerHTML='<div class="alert-err">'+esc(d.error||'failed')+'</div>';
  log('ERROR '+(d.error||'failed'));
 }
}
if(window.orca&&window.orca.onMessage){window.orca.onMessage(function(d){
 if(!d)return;
 if(d.type==='progress'){setStatus(d.message||'working…');if(d.message)log(d.message);return;}
 if(d.type==='result'&&d.ok){setStatus('done');pvSet(d.preview);showResult(d);return;}
 if(d.type==='error'||d.ok===false){setStatus('failed');pvSet(null);showResult({ok:false,error:d.error});return;}
 log(String(JSON.stringify(d)).slice(0,2000));
});}
(function init(){
 var sel=document.getElementById('exSel');
 sel.innerHTML=Object.keys(EXAMPLES).map(function(k){return '<option value="'+k+'">'+k+'</option>';}).join('');
 buildObjs();
 initMonaco();
 pvDraw();
})();
</script>
</body>
</html>
"""


# ---------------------------------------------------------------------------
# Plugin wiring (only when running inside OrcaSlicer)
# ---------------------------------------------------------------------------

def _handle_message_sync(capability, msg):
    """Route one JS message; heavy work runs in a worker thread."""
    if not isinstance(msg, dict):
        return {"type": "error", "ok": False, "error": "message must be an object"}
    cmd = msg.get("command")
    if cmd == "ping":
        return {"type": "pong", "ok": True}
    if cmd in ("run", "generate"):
        try:
            export_format = str(msg.get("format", "stl")).lower()
            tolerance = float(msg.get("tolerance", DEFAULT_TOLERANCE))
        except Exception:
            return {"type": "error", "ok": False, "error": "bad format/tolerance"}
        if cmd == "generate":
            try:
                code = generate_primitive_code(
                    str(msg.get("primitive", "box")), dict(msg.get("params", {})))
            except Exception as exc:
                return {"type": "error", "ok": False, "error": str(exc)}
            stem = str(msg.get("filename") or msg.get("primitive") or "model")
        else:
            code = str(msg.get("code", ""))
            if not code.strip():
                return {"type": "error", "ok": False, "error": "code is empty"}
            stem = str(msg.get("filename") or "model")

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
                        try:
                            msg = json.loads(msg)
                        except Exception:
                            pass
                    res = _handle_message_sync(self, msg)
                    # Immediate ack for sync commands; worker posts final result.
                    if isinstance(res, dict) and res.get("type") == "progress" \
                            and res.get("message") == "Started…":
                        pass  # worker will post updates; no need to echo
                    elif isinstance(res, dict):
                        self.post_message(res)
                except Exception as exc:
                    try:
                        self.post_message({"type": "error", "ok": False,
                                           "error": f"handler failed: {exc}"})
                    except Exception:
                        pass

            def get_default_config(self):
                return {"tolerance": DEFAULT_TOLERANCE, "format": "stl"}

        @orca.plugin
        class OrcadPlugin(orca.base):
            def register_capabilities(self):
                orca.register_capability(CadPage)
    else:
        # Fallback for Orca builds without orca.pages: visible upgrade hint.
        class CadScriptFallback(orca.script.ScriptPluginCapabilityBase):
            def get_name(self):
                return "orcad (needs Pages build)"

            def execute(self):
                return orca.ExecutionResult.failure(
                    orca.PluginResult.RecoverableError,
                    "orcad needs OrcaSlicer Nightly with orca.pages "
                    "(Pages tab API). Please update OrcaSlicer.",
                )

        @orca.plugin
        class OrcadPlugin(orca.base):
            def register_capabilities(self):
                orca.register_capability(CadScriptFallback)
