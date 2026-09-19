# /// script
# requires-python = ">=3.12"
# dependencies = ["build123d", "numpy"]
#
# [tool.orcaslicer.plugin]
# name = "OrcaCAD"
# description = "build123d CAD tab for OrcaSlicer: parametric primitives + Monaco code editor + live 3D preview, export STL/STEP/3MF."
# author = "OrcaCadPlugin"
# version = "0.2.0"
# ///
"""OrcaCAD — build123d CAD tab (Pages capability).

Top-level "CAD" tab next to Prepare/Preview/Device/Project (same mechanism
as a FilamentHub-style tab): implemented as orca.pages.PagesPluginCapabilityBase.

Layout (v0.2):
- Left: tabbed panel switching between "Primitives" and "Code Editor" (Monaco,
  textarea fallback when the CDN is unreachable).
- Right: persistent 3D preview (always visible) + result + log.
- Exports go to <plugin_dir>/exports (inside Orca data_dir, no audit prompt).
  Manual import into plater for v0.2 (orca.host is read-only): drag the
  exported file onto the plater.

UI stack: Bootstrap 5 + Monaco + a dependency-free canvas 3D renderer via CDN
(jsdelivr), with offline fallbacks (unstyled-but-usable layout, textarea
editor, stats-only preview) so the tab never breaks without network.

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

PLUGIN_VERSION = "0.2.0"
EXPORT_FORMATS = ("stl", "step", "3mf")
DEFAULT_TOLERANCE = 0.001
PREVIEW_MAX_TRIS = 3000  # cap on triangles sent to the page for preview

# ---------------------------------------------------------------------------
# Pure logic (no orca / build123d import required — unit-testable)
# ---------------------------------------------------------------------------

PRIMITIVES = {
    "box": {
        "label": "Box",
        "params": [
            {"key": "L", "label": "Length (mm)", "min": 1, "max": 300, "step": 0.5, "default": 20},
            {"key": "W", "label": "Width (mm)", "min": 1, "max": 300, "step": 0.5, "default": 20},
            {"key": "H", "label": "Height (mm)", "min": 1, "max": 300, "step": 0.5, "default": 20},
        ],
    },
    "cylinder": {
        "label": "Cylinder",
        "params": [
            {"key": "R", "label": "Radius (mm)", "min": 0.5, "max": 150, "step": 0.5, "default": 10},
            {"key": "H", "label": "Height (mm)", "min": 1, "max": 300, "step": 0.5, "default": 20},
        ],
    },
    "tube": {
        "label": "Tube",
        "params": [
            {"key": "R_OUT", "label": "Outer radius (mm)", "min": 1, "max": 150, "step": 0.5, "default": 12},
            {"key": "R_IN", "label": "Inner radius (mm)", "min": 0.5, "max": 149, "step": 0.5, "default": 8},
            {"key": "H", "label": "Height (mm)", "min": 1, "max": 300, "step": 0.5, "default": 25},
        ],
    },
    "bracket": {
        "label": "Bracket plate + 2 holes",
        "params": [
            {"key": "L", "label": "Length (mm)", "min": 10, "max": 300, "step": 0.5, "default": 60},
            {"key": "W", "label": "Width (mm)", "min": 10, "max": 200, "step": 0.5, "default": 30},
            {"key": "T", "label": "Thickness (mm)", "min": 1, "max": 50, "step": 0.5, "default": 5},
            {"key": "D", "label": "Hole dia (mm)", "min": 1, "max": 50, "step": 0.5, "default": 5},
        ],
    },
}

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
}


def _num(v, name):
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise ValueError(f"{name} must be a number, got {v!r}")
    if not (f == f and abs(f) != float("inf")):
        raise ValueError(f"{name} must be finite")
    return f


def validate_primitive_params(primitive, params):
    """Raise ValueError on bad params; return cleaned dict of floats."""
    if primitive not in PRIMITIVES:
        raise ValueError(f"unknown primitive {primitive!r}")
    spec = PRIMITIVES[primitive]["params"]
    cleaned = {}
    for p in spec:
        key = p["key"]
        if key not in params:
            raise ValueError(f"missing param {key}")
        v = _num(params[key], key)
        if v < p["min"] or v > p["max"]:
            raise ValueError(f"{key}={v} out of range [{p['min']}, {p['max']}]")
        cleaned[key] = v
    if primitive == "tube" and not cleaned["R_IN"] < cleaned["R_OUT"]:
        raise ValueError("R_IN must be smaller than R_OUT")
    if primitive == "bracket":
        if cleaned["D"] >= min(cleaned["L"] / 2, cleaned["W"]):
            raise ValueError("Hole diameter D too large for plate size")
    return cleaned


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
    raise ValueError(f"unknown primitive {primitive!r}")


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
# Self-contained page UI.
# Bootstrap 5 + Monaco + dependency-free canvas 3D renderer via CDN
# (jsdelivr), with offline fallbacks so the tab never breaks: unstyled-but-
# usable layout, plain textarea editor, stats-only preview.
# Orca theme is mapped onto Bootstrap vars (--orca-*) where available.
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>OrcaCAD</title>
<link href="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/css/bootstrap.min.css" rel="stylesheet">
<style>
:root{
  --bg:var(--orca-bg,#f8f9fa); --fg:var(--orca-fg,#212529);
  --muted:var(--orca-muted,#6c757d); --border:var(--orca-border,#dee2e6);
  --accent:var(--orca-accent,#0d6efd); --accent-fg:var(--orca-accent-fg,#fff);
  --ui:var(--orca-font,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
  --bs-body-bg:var(--bg); --bs-body-color:var(--fg);
  --bs-border-color:var(--border); --bs-primary:var(--accent);
  --bs-link-color:var(--accent);
}
body{background:var(--bg);color:var(--fg);font-family:var(--ui)}
.mono{font-family:var(--mono)}
.topbar{border-bottom:1px solid var(--border)}
.nav-tabs .nav-link{color:var(--muted)}
.nav-tabs .nav-link.active{color:var(--fg);font-weight:600}
.btn-primary{--bs-btn-bg:var(--accent);--bs-btn-border-color:var(--accent);--bs-btn-color:var(--accent-fg)}
.card{background:var(--bg);border-color:var(--border)}
.text-muted{color:var(--muted)!important}
#editor{height:340px;border:1px solid var(--border);border-radius:.375rem;overflow:hidden}
#code{width:100%;min-height:340px;font-family:var(--mono);font-size:12px}
#pv3d{width:100%;height:360px;border:1px solid var(--border);border-radius:.375rem;cursor:grab;touch-action:none}
.log{font-family:var(--mono);font-size:12px;max-height:200px;overflow:auto;white-space:pre-wrap}
.prim{border:1px solid var(--border);border-radius:.5rem;padding:.6rem .75rem;margin-bottom:.5rem;cursor:pointer}
.prim.active{border-color:var(--accent);box-shadow:0 0 0 .15rem color-mix(in srgb,var(--accent) 25%,transparent)}
input[type=range]{accent-color:var(--accent)}
/* offline fallback layout if Bootstrap CSS fails: keep two columns usable */
@supports not (display:grid){.row{display:flex;flex-wrap:wrap}}
</style>
</head>
<body>
<nav class="navbar topbar px-3 py-2 sticky-top" style="background:var(--bg)">
  <span class="navbar-brand mb-0 h1">OrcaCAD <small class="text-muted fw-normal">build123d · v0.2</small></span>
  <span id="status" class="text-muted small me-auto"></span>
  <div class="d-flex gap-2 align-items-center flex-wrap">
    <select id="fmt" class="form-select form-select-sm" style="width:auto" title="Export format">
      <option value="stl">STL</option><option value="step">STEP</option><option value="3mf">3MF</option>
    </select>
    <input id="tol" type="number" class="form-control form-control-sm" style="width:90px" value="0.001" step="0.001" min="0.0001" max="1" title="STL tessellation tolerance">
    <button id="runBtn" class="btn btn-primary btn-sm" onclick="runActive()">Run + Export</button>
  </div>
</nav>

<div class="container-fluid py-3">
<div class="row g-3">
  <!-- LEFT: switchable panel -->
  <div class="col-12 col-lg-4">
    <ul class="nav nav-tabs" role="tablist">
      <li class="nav-item" role="presentation"><button id="tabbtn-prims" class="nav-link active" onclick="switchLeft('prims')">Primitives</button></li>
      <li class="nav-item" role="presentation"><button id="tabbtn-editor" class="nav-link" onclick="switchLeft('editor')">Code Editor</button></li>
    </ul>
    <div id="pane-prims" class="pt-3">
      <div id="prims"></div>
    </div>
    <div id="pane-editor" class="pt-3" style="display:none">
      <div class="card"><div class="card-body">
        <p class="small text-muted">Algebra mode. Assign the final solid to <span class="mono">result</span>.</p>
        <div id="editor"></div>
        <textarea id="code" class="form-control mt-2" style="display:none" spellcheck="false"></textarea>
        <div class="d-flex gap-2 mt-2 flex-wrap align-items-center">
          <input id="fname" class="form-control form-control-sm" style="width:150px" value="model" title="File name">
          <select id="exSel" class="form-select form-select-sm" style="width:auto"></select>
          <button class="btn btn-outline-secondary btn-sm" onclick="loadExample()">Load example</button>
        </div>
      </div></div>
    </div>
    <div class="card mt-3"><div class="card-body">
      <h6 class="card-subtitle mb-2 text-muted">Manual import (v0.2)</h6>
      <p class="card-text small">Plugins can't push models into the plater yet — drag the exported file onto Prepare to slice it.</p>
    </div></div>
  </div>
  <!-- RIGHT: always-on preview -->
  <div class="col-12 col-lg-8">
    <div class="card"><div class="card-body">
      <div class="d-flex align-items-center gap-2 flex-wrap mb-2">
        <h5 class="card-title mb-0">Preview</h5>
        <span id="pvInfo" class="text-muted small">nothing rendered yet — run a model</span>
        <span class="ms-auto"></span>
        <button class="btn btn-outline-secondary btn-sm" onclick="pvReset()">Reset view</button>
        <button id="wireBtn" class="btn btn-outline-secondary btn-sm" onclick="pvToggleWire()">Wireframe: off</button>
        <button id="spinBtn" class="btn btn-outline-secondary btn-sm" onclick="pvToggleSpin()">Spin: on</button>
      </div>
      <canvas id="pv3d"></canvas>
      <div id="pvStats" class="mono small text-muted mt-2">—</div>
    </div></div>
    <div class="card mt-3"><div class="card-body">
      <h5 class="card-title">Result</h5><div id="result" class="text-muted">Nothing exported yet.</div>
    </div></div>
    <div class="card mt-3"><div class="card-body">
      <h5 class="card-title">Log</h5><div id="log" class="log card card-body">ready.
</div>
    </div></div>
  </div>
</div>
</div>

<script src="https://cdn.jsdelivr.net/npm/bootstrap@5.3.3/dist/js/bootstrap.bundle.min.js"></script>
<script src="https://cdn.jsdelivr.net/npm/monaco-editor@0.49.0/min/vs/loader.js"></script>
<script>
'use strict';
/* ---------------- state ---------------- */
var S={left:'prims',prim:'box',params:{}};
var PRIMS={
 box:{label:'Box',params:[['L','Length',20],['W','Width',20],['H','Height',20]]},
 cylinder:{label:'Cylinder',params:[['R','Radius',10],['H','Height',20]]},
 tube:{label:'Tube',params:[['R_OUT','Outer R',12],['R_IN','Inner R',8],['H','Height',25]]},
 bracket:{label:'Bracket',params:[['L','Length',60],['W','Width',30],['T','Thick',5],['D','Hole dia',5]]}
};
var EXAMPLES={
 calibration_cube:'from build123d import *\nresult = Box(20, 20, 20)\n',
 bracket:'from build123d import *\nL, W, T, D = 60, 30, 5, 5\nplate = Box(L, W, T)\nhole = Cylinder(D / 2, T + 2)\nh1 = Pos(-L / 4, 0, -1) * hole\nh2 = Pos(L / 4, 0, -1) * hole\nresult = plate - h1 - h2\n',
 tube_demo:'from build123d import *\nouter = Cylinder(12, 25)\ninner = Cylinder(8, 27)\ntube = outer - inner\nring = Pos(0, 0, 25) * Cylinder(10, 3)\nresult = tube + ring\n'
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
 document.getElementById('pane-prims').style.display=which==='prims'?'':'none';
 document.getElementById('pane-editor').style.display=which==='editor'?'':'none';
 document.getElementById('tabbtn-prims').classList.toggle('active',which==='prims');
 document.getElementById('tabbtn-editor').classList.toggle('active',which==='editor');
 if(which==='editor')setTimeout(monacoLayout,30);
}
function runActive(){ if(S.left==='prims')generate(); else runEditor(); }
/* ---------------- primitives ---------------- */
function buildPrims(){
 var host=document.getElementById('prims');host.innerHTML='';
 Object.keys(PRIMS).forEach(function(k){
  var d=document.createElement('div');d.className='prim'+(S.prim===k?' active':'');
  var t=document.createElement('div');t.innerHTML='<b>'+esc(PRIMS[k].label)+'</b>';
  t.onclick=function(){S.prim=k;S.params={};buildPrims();};d.appendChild(t);
  if(S.prim===k){
   PRIMS[k].params.forEach(function(p){
    var key=p[0],lab=p[1],def=p[2];
    var val=(S.params[key]!=null)?S.params[key]:def;S.params[key]=val;
    var row=document.createElement('div');row.className='d-flex gap-2 align-items-center mt-1 flex-wrap';
    var lab2=document.createElement('small');lab2.className='text-muted';lab2.textContent=key+' '+lab;lab2.style.minWidth='90px';
    var num=document.createElement('input');num.type='number';num.className='form-control form-control-sm';num.style.width='80px';num.value=val;num.step='0.5';
    var rng=document.createElement('input');rng.type='range';rng.className='flex-grow-1';rng.min='0.5';rng.max=Math.max(def*3,50);rng.step='0.5';rng.value=val;
    num.oninput=function(){S.params[key]=parseFloat(num.value);rng.value=num.value;};
    rng.oninput=function(){S.params[key]=parseFloat(rng.value);num.value=rng.value;};
    row.appendChild(lab2);row.appendChild(num);row.appendChild(rng);d.appendChild(row);
   });
   var b=document.createElement('button');b.className='btn btn-primary btn-sm mt-2';b.textContent='Generate + Export';
   b.onclick=function(ev){ev.stopPropagation();generate();};d.appendChild(b);
  }
  host.appendChild(d);
 });
 var sel=document.getElementById('exSel');
 sel.innerHTML=Object.keys(EXAMPLES).map(function(k){return '<option value="'+k+'">'+k+'</option>';}).join('');
}
function generate(){
 setStatus('working…');log('primitive '+S.prim+' '+JSON.stringify(S.params));
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
 PV.ext={c:[(mnx+mxx)/2,(mny+mxy)/2,(mnz+mxz)/2],
  d:Math.max(mxx-mnx,mxy-mny,mxz-mnz,1e-6)};
}
function pvSet(p){
 PV.tris=(p&&p.tris)||[];PV.total=(p&&p.total)||0;pvFit();pvDraw();
 var info=document.getElementById('pvInfo');
 info.textContent=PV.tris.length?('mesh: '+PV.total+' tris'+(PV.total>PV.tris.length/9?' (decimated preview)':'')):'preview unavailable — stats only';
}
function pvDraw(){
 var cv=document.getElementById('pv3d');if(!cv||!cv.clientWidth)return;
 var dpr=window.devicePixelRatio||1,W=cv.clientWidth,H=cv.clientHeight;
 if(cv.width!==W*dpr||cv.height!==H*dpr){cv.width=W*dpr;cv.height=H*dpr;}
 var ctx=cv.getContext('2d');ctx.setTransform(dpr,0,0,dpr,0,0);ctx.clearRect(0,0,W,H);
 var t=PV.tris;
 ctx.fillStyle=pvCss('--muted','#6c757d');ctx.font='12px sans-serif';
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
 var base=pvCss('--accent','#0d6efd');
 function shade(nx,ny,nz){
  var d=nx*lx+ny*ly+nz*lz;if(d<0)d=0;
  var k=0.35+0.65*d;return 'rgb('+Math.round(120*k+40)+','+Math.round(150*k+40)+','+Math.round(220*k+30)+')';
 }
 void base;
 for(j=0;j<n;j++){
  i=order[j]*9;
  var ax=P[i],ay=P[i+1],az=P[i+2],bx=P[i+3],by=P[i+4],bz=P[i+5],cx2=P[i+6],cy2=P[i+7],cz=P[i+8];
  // face normal in view space via rotated (not projected) coords: recompute cheaply
  var ux=bx-ax,uy=by-ay,uz=bz-az,vx=cx2-ax,vy=cy2-ay,vz=cz-az;
  var nx=uy*vz-uz*vy,ny=uz*vx-ux*vz,nz=ux*vy-uy*vx;
  var nl=Math.sqrt(nx*nx+ny*ny+nz*nz)||1;nx/=nl;ny/=nl;nz/=nl;
  ctx.beginPath();ctx.moveTo(ax,ay);ctx.lineTo(bx,by);ctx.lineTo(cx2,cy2);ctx.closePath();
  if(PV.wire){ctx.strokeStyle=pvCss('--accent','#0d6efd');ctx.lineWidth=0.7;ctx.stroke();}
  else{ctx.fillStyle=shade(nx,ny,nz);ctx.fill();ctx.strokeStyle='rgba(0,0,0,0.12)';ctx.lineWidth=0.4;ctx.stroke();}
 }
}
function pvReset(){PV.yaw=0.7;PV.pitch=0.55;PV.zoom=1;pvDraw();}
function pvToggleWire(){PV.wire=!PV.wire;document.getElementById('wireBtn').textContent='Wireframe: '+(PV.wire?'on':'off');pvDraw();}
function pvToggleSpin(){PV.spin=!PV.spin;document.getElementById('spinBtn').textContent='Spin: '+(PV.spin?'on':'off');}
(function(){
 var cv=document.getElementById('pv3d'),drag=null;
 cv.addEventListener('pointerdown',function(e){drag={x:e.clientX,y:e.clientY};cv.setPointerCapture(e.pointerId);cv.style.cursor='grabbing';});
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
 var st=document.getElementById('pvStats');
 st.textContent='stats: '+JSON.stringify(d.stats||{});
 if(d.ok){
  el.innerHTML='<dl class="row mb-0">'
   +'<dt class="col-sm-2">File</dt><dd class="col-sm-10 mono">'+esc(d.file)+'</dd>'
   +'<dt class="col-sm-2">Size</dt><dd class="col-sm-10 mono">'+esc(d.size_bytes)+' bytes</dd>'
   +'<dt class="col-sm-2">Solid</dt><dd class="col-sm-10 mono">'+esc(d.var||'result')+'</dd></dl>'
   +'<p class="text-muted small mb-0">Drag this file onto Prepare to slice it.</p>';
  log('OK '+d.filename+' ('+d.size_bytes+' B)');
 }else{
  el.innerHTML='<div class="alert alert-danger mb-0 mono">'+esc(d.error||'failed')+'</div>';
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
buildPrims();
initMonaco();
pvDraw();
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
                return "CAD"

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
        class OrcaCadPlugin(orca.base):
            def register_capabilities(self):
                orca.register_capability(CadPage)
    else:
        # Fallback for Orca builds without orca.pages: visible upgrade hint.
        class CadScriptFallback(orca.script.ScriptPluginCapabilityBase):
            def get_name(self):
                return "OrcaCAD (needs Pages build)"

            def execute(self):
                return orca.ExecutionResult.failure(
                    orca.PluginResult.RecoverableError,
                    "OrcaCAD needs OrcaSlicer Nightly with orca.pages "
                    "(Pages tab API). Please update OrcaSlicer.",
                )

        @orca.plugin
        class OrcaCadPlugin(orca.base):
            def register_capabilities(self):
                orca.register_capability(CadScriptFallback)
