# /// script
# requires-python = ">=3.12"
# dependencies = ["build123d", "numpy"]
#
# [tool.orcaslicer.plugin]
# name = "OrcaCAD"
# description = "build123d CAD tab for OrcaSlicer: parametric primitives + code editor, export STL/STEP/3MF."
# author = "OrcaCadPlugin"
# version = "0.1.0"
# ///
"""OrcaCAD — build123d CAD tab (Pages capability).

Top-level "CAD" tab next to Prepare/Preview/Device/Project (same mechanism
as a FilamentHub-style tab): implemented as orca.pages.PagesPluginCapabilityBase.

- Primitives (Box / Cylinder / Tube / Bracket) with sliders -> generates
  build123d algebra-mode code, runs it, exports STL/STEP/3MF.
- Code editor: type any build123d algebra snippet that assigns the final
  solid to `result`, run + export.
- Exports go to <plugin_dir>/exports (inside Orca data_dir, no audit prompt).
  Manual import into plater for v0.1 (orca.host is read-only): drag the
  exported file onto the plater.

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

PLUGIN_VERSION = "0.1.0"
EXPORT_FORMATS = ("stl", "step", "3mf")
DEFAULT_TOLERANCE = 0.001

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
    }


# ---------------------------------------------------------------------------
# Self-contained page UI (no external resources; themed via --orca-*)
# ---------------------------------------------------------------------------

PAGE_HTML = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
:root{
  --bg:var(--orca-bg,#fff); --fg:var(--orca-fg,#1f2429);
  --muted:var(--orca-muted,#6b7580); --border:var(--orca-border,#d9dee3);
  --accent:var(--orca-accent,#009688); --accent-fg:var(--orca-accent-fg,#fff);
  --ui:var(--orca-font,system-ui,-apple-system,'Segoe UI',Roboto,sans-serif);
  --mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);font:13px/1.5 var(--ui)}
header{display:flex;gap:10px;align-items:center;padding:10px 14px;border-bottom:1px solid var(--border);flex-wrap:wrap}
header h1{font-size:15px;margin:0}
header .sub{color:var(--muted);font-size:12px}
main{display:grid;grid-template-columns:300px 1fr;gap:0;min-height:calc(100vh - 53px)}
aside{border-right:1px solid var(--border);padding:12px;overflow:auto}
section{padding:12px 14px;overflow:auto}
.card{border:1px solid var(--border);border-radius:8px;padding:10px 12px;margin-bottom:10px}
.card h3{margin:0 0 8px;font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted)}
button{font:inherit;padding:5px 12px;border-radius:6px;background:var(--accent);color:var(--accent-fg);border:1px solid var(--accent);cursor:pointer}
button.secondary{background:transparent;color:var(--fg);border-color:var(--border)}
button:disabled{opacity:.55}
input,select,textarea{font:inherit;color:var(--fg);background:var(--bg);border:1px solid var(--border);border-radius:6px;padding:5px 8px}
textarea{width:100%;min-height:220px;font-family:var(--mono);font-size:12px;white-space:pre}
.row{display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin:6px 0}
label{font-size:12px;color:var(--muted)}
label b{color:var(--fg);font-weight:600}
input[type=range]{flex:1}
.kv{display:grid;grid-template-columns:130px 1fr;gap:3px 10px;font-size:12px}
.kv .k{color:var(--muted)} .kv .v{font-family:var(--mono);word-break:break-word}
.muted{color:var(--muted)} .mono{font-family:var(--mono)}
.log{font-family:var(--mono);font-size:12px;border:1px solid var(--border);border-radius:6px;padding:8px 10px;max-height:220px;overflow:auto;white-space:pre-wrap}
.banner{border:1px solid var(--border);border-left:3px solid var(--accent);border-radius:6px;padding:8px 10px;margin-bottom:10px;font-size:12px}
.prim{border:1px solid var(--border);border-radius:8px;padding:8px 10px;margin-bottom:8px;cursor:pointer}
.prim.active{border-color:var(--accent)}
@media (max-width:800px){main{grid-template-columns:1fr}aside{border-right:0;border-bottom:1px solid var(--border)}}
</style>
</head>
<body>
<header>
  <h1>OrcaCAD</h1><span class="sub">build123d CAD tab · v0.1</span>
  <span style="flex:1"></span>
  <label>Format <select id="fmt"><option value="stl">STL</option><option value="step">STEP</option><option value="3mf">3MF</option></select></label>
  <label title="STL tessellation tolerance">Tolerance <input id="tol" type="number" value="0.001" step="0.001" min="0.0001" max="1" style="width:90px"></label>
  <button id="runBtn" onclick="runEditor()">Run + Export</button>
</header>
<main>
<aside>
  <div class="card"><h3>Parametric primitives</h3><div id="prims"></div></div>
  <div class="card"><h3>Examples</h3><div id="examples"></div></div>
  <div class="card"><h3>Manual import (v0.1)</h3>
    <div class="muted">Orca plugins cannot push models into the plater yet. After export, drag the file from the exports folder onto the Prepare view.</div>
  </div>
</aside>
<section>
  <div class="banner">Assign your final solid to <span class="mono">result</span> — e.g. <span class="mono">result = Box(20,20,20)</span>. Algebra mode only in v0.1.</div>
  <div class="row">
    <label>File name <input id="fname" value="model" style="width:160px"></label>
    <button class="secondary" onclick="loadExample()">Load example</button>
    <select id="exSel"></select>
    <span class="muted" id="status"></span>
  </div>
  <textarea id="code" spellcheck="false"></textarea>
  <div class="card" style="margin-top:10px"><h3>Result</h3><div id="result" class="muted">Nothing exported yet.</div></div>
  <div class="card"><h3>Log</h3><div id="log" class="log">ready.
</div></div>
</section>
</main>
<script>
'use strict';
var S={prim:'box',params:{}};
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
function esc(s){return String(s==null?'':s).replace(/[&<>"']/g,function(c){return {'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c];});}
function log(m){var el=document.getElementById('log');el.textContent+=m+'\n';el.scrollTop=el.scrollHeight;}
function setStatus(m){document.getElementById('status').textContent=m;}
function send(o){try{window.orca.postMessage(o);}catch(e){log('bridge error: '+e);}}
function buildPrims(){
 var host=document.getElementById('prims');host.innerHTML='';
 Object.keys(PRIMS).forEach(function(k){
  var d=document.createElement('div');d.className='prim'+(S.prim===k?' active':'');
  d.innerHTML='<b>'+esc(PRIMS[k].label)+'</b><div class="muted">click to edit, then Generate</div>';
  d.onclick=function(){S.prim=k;S.params={};buildPrims();};
  if(S.prim===k){
   PRIMS[k].params.forEach(function(p){
    var key=p[0],lab=p[1],def=p[2];
    var val=(S.params[key]!=null)?S.params[key]:def;
    var row=document.createElement('div');row.className='row';
    row.innerHTML='<label><b>'+esc(key)+'</b> '+esc(lab)+'</label>';
    var num=document.createElement('input');num.type='number';num.value=val;num.step='0.5';num.style.width='80px';
    num.oninput=function(){S.params[key]=parseFloat(num.value);rng.value=num.value;};
    var rng=document.createElement('input');rng.type='range';rng.min='0.5';rng.max=Math.max(def*3,50);rng.step='0.5';rng.value=val;
    rng.oninput=function(){S.params[key]=parseFloat(rng.value);num.value=rng.value;};
    S.params[key]=val;
    row.appendChild(num);row.appendChild(rng);d.appendChild(row);
   });
   var b=document.createElement('button');b.textContent='Generate + Export '+PRIMS[k].label;
   b.onclick=function(ev){ev.stopPropagation();generate();};
   d.appendChild(b);
  }
  host.appendChild(d);
 });
 var sel=document.getElementById('exSel');
 sel.innerHTML=Object.keys(EXAMPLES).map(function(k){return '<option value="'+k+'">'+k+'</option>';}).join('');
 document.getElementById('examples').innerHTML=Object.keys(EXAMPLES).map(function(k){
  return '<div class="row"><button class="secondary" data-ex="'+k+'">Load</button><span class="mono">'+esc(k)+'</span></div>';
 }).join('');
 Array.prototype.forEach.call(document.querySelectorAll('[data-ex]'),function(btn){
  btn.onclick=function(){document.getElementById('code').value=EXAMPLES[btn.getAttribute('data-ex')];log('loaded '+btn.getAttribute('data-ex'));};
 });
 if(!document.getElementById('code').value)document.getElementById('code').value=EXAMPLES.calibration_cube;
}
function fmt(){return document.getElementById('fmt').value;}
function tol(){return parseFloat(document.getElementById('tol').value)||0.001;}
function generate(){
 setStatus('working…');log('primitive '+S.prim+' '+JSON.stringify(S.params));
 send({command:'generate',primitive:S.prim,params:S.params,format:fmt(),tolerance:tol(),filename:S.prim});
}
function runEditor(){
 var code=document.getElementById('code').value;
 var fn=document.getElementById('fname').value||'model';
 setStatus('working…');log('run '+code.length+' chars -> '+fn+'.'+fmt());
 send({command:'run',code:code,format:fmt(),tolerance:tol(),filename:fn});
}
function loadExample(){var k=document.getElementById('exSel').value;document.getElementById('code').value=EXAMPLES[k];log('loaded '+k);}
if(window.orca&&window.orca.onMessage){window.orca.onMessage(function(d){
 if(!d)return;
 if(d.type==='progress'){setStatus(d.message||'working…');if(d.message)log(d.message);return;}
 if(d.type==='result'&&d.ok){
  setStatus('done');
  document.getElementById('result').innerHTML='<div class="kv">'
   +'<div class="k">file</div><div class="v">'+esc(d.file)+'</div>'
   +'<div class="k">size</div><div class="v">'+esc(d.size_bytes)+' bytes</div>'
   +'<div class="k">var</div><div class="v">'+esc(d.var||'result')+'</div>'
   +'<div class="k">stats</div><div class="v">'+esc(JSON.stringify(d.stats||{}))+'</div></div>'
   +'<p class="muted">Drag this file onto Prepare to slice it.</p>';
  log('OK '+d.filename+' ('+d.size_bytes+' B)');
  return;
 }
 if(d.type==='error'||d.ok===false){setStatus('failed');document.getElementById('result').innerHTML='<span style="color:#c00">'+esc(d.error||'failed')+'</span>';log('ERROR '+(d.error||'failed'));return;}
 log(JSON.stringify(d).slice(0,2000));
});}
buildPrims();
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
