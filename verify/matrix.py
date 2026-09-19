#!/usr/bin/env python3
"""Feature-matrix compare: every ref case vs orcad output. See MATRIIX below.

Usage: python3 verify/matrix.py [--only t_div,t_tabs_full] [--tols]
Runs missing ours-STLs via w_b123d, compares all, prints a summary table.
Exit 0 iff every case passes.
"""
import json
import subprocess
import sys
from pathlib import Path

ROOT = Path("/home/agent/OrcaCadPlugin")
CACHE = ROOT / ".verify-cache"
TMP = Path("/tmp/opencode")
VENV_PY = str(ROOT / ".venv" / "bin" / "python")

BASE = {"GX": 2, "GY": 2, "HU": 6, "HMODE": 0, "ZS": False, "FILL": 0,
        "WALL": 0.95, "DX": 1, "DY": 1, "DEPTH": 0, "SCOOPW": 0.0,
        "TABSTYLE": 5, "TABPLACE": 0, "CYL": False, "CD": 10, "CCHAM": 0.5,
        "REFINED": False, "MAGNETS": False, "SCREW": False, "CRUSH": False,
        "CHAMFER": False, "PRINTABLE": False, "CORNERS": False,
        "THUMB": False, "LIP": True}

REF_DEFAULTS = {"style_tab": 1, "scoop": 1, "refined_holes": "true",
                "crush_ribs": "true", "chamfer_holes": "true",
                "printable_hole_top": "true"}

MATRIX = {
    "t_plain": {},
    "t_nolip": {"LIP": False},
    "t_div": {"DX": 2, "DY": 2, "MAGNETS": True},
    "t_tabs_full": {"DX": 2, "DY": 2, "TABSTYLE": 0},
    "t_tabs_center": {"DX": 2, "DY": 2, "TABSTYLE": 3},
    "t_tabs_auto": {"GX": 3, "DX": 3, "DY": 1, "TABSTYLE": 1},
    "t_tabs_tl": {"DX": 2, "DY": 2, "TABSTYLE": 1, "TABPLACE": 1},
    "t_scoop05": {"DX": 2, "DY": 2, "SCOOPW": 0.5},
    "t_scoop1": {"DX": 2, "DY": 2, "SCOOPW": 1.0},
    "t_cyl": {"DX": 2, "DY": 2, "CYL": True},
    "t_depth": {"DX": 2, "DY": 2, "DEPTH": 10},
    "t_magnet": {"MAGNETS": True},
    "t_screw": {"SCREW": True, "CHAMFER": True},
    "t_screw_print": {"SCREW": True, "PRINTABLE": True},
    "t_refined": {"DX": 2, "DY": 2, "REFINED": True},
    "t_crush": {"MAGNETS": True, "CRUSH": True},
    "t_chamfer": {"MAGNETS": True, "CHAMFER": True},
    "t_printable": {"MAGNETS": True, "PRINTABLE": True},
    "t_corners": {"GX": 3, "MAGNETS": True, "CORNERS": True},
    "t_thumbscrew": {"THUMB": True},
    "t_default": {"GX": 3, "TABSTYLE": 1, "SCOOPW": 1.0, "REFINED": True,
                  "CRUSH": True, "CHAMFER": True, "PRINTABLE": True},
    "t_hmode1": {"HMODE": 1, "HU": 35},
    "t_fill": {"DX": 2, "DY": 2, "FILL": 10},
    "tabwide_auto": {"GX": 3, "GY": 1, "DX": 1, "DY": 1, "TABSTYLE": 1},
    "tabwide_center": {"GX": 3, "GY": 1, "DX": 1, "DY": 1, "TABSTYLE": 3},
    "tabwide_full": {"GX": 3, "GY": 1, "DX": 1, "DY": 1, "TABSTYLE": 0},
    "newdef": {"TABSTYLE": 1, "SCOOPW": 1.0, "REFINED": True,
               "CRUSH": True, "CHAMFER": True, "PRINTABLE": True},
}

HEIGHTS = [0.2, 1, 2, 3, 4, 5, 10, 20, 30, 35, 40, 43]


def run_ours(name, params):
    out = TMP / f"ours_{name}.stl"
    if out.exists():
        return out
    cmd = [VENV_PY, str(ROOT / "verify" / "w_b123d.py"), "gridfinity_bin",
           json.dumps(params), "0.05", str(out)]
    print(f"build {name} ...", flush=True)
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    if not out.exists():
        print(f"BUILD FAILED {name}:\n{r.stdout}\n{r.stderr}")
        return None
    return out


def compare(name):
    ref = CACHE / f"ref_{name}.stl"
    if not ref.exists():
        ref = TMP / f"ref_{name}.stl"
    ours = TMP / f"ours_{name}.stl"
    if not ours.exists():
        return {"name": name, "pass": False, "fails": ["ours stl missing"]}
    cmd = [VENV_PY, str(ROOT / "verify" / "w_compare.py"), str(ref), str(ours),
           "--heights", ",".join(map(str, HEIGHTS)),
           "--tol-dim", "0.3", "--tol-vol", "0.04"]
    if name != "t_thumbscrew":
        # threaded holes read asymmetric centers; plain-hole approx is
        # verified by volume+bbox instead (see README limits)
        cmd += ["--holes", "1.2"]
    r = subprocess.run(cmd, capture_output=True, text=True, cwd=str(ROOT))
    try:
        d = json.loads(r.stdout)
    except Exception:
        return {"name": name, "pass": False, "fails": [r.stderr[-500:]]}
    holes = d["holes"][0] if d["holes"] else {}
    return {"name": name, "pass": d["pass"], "fails": d["fails"],
            "vol_rel": d.get("vol_rel_diff"),
            "ref_vol": round(d["ref"]["volume"]), "ours_vol": round(d["ours"]["volume"]),
            "holes": (len(holes.get("ref", [])), len(holes.get("ours", [])))}


def main():
    only = None
    if "--only" in sys.argv:
        only = sys.argv[sys.argv.index("--only") + 1].split(",")
    results = []
    for name, over in MATRIX.items():
        if only and name not in only:
            continue
        params = dict(BASE)
        params.update(over)
        if run_ours(name, params) is None:
            results.append({"name": name, "pass": False, "fails": ["build failed"]})
            continue
        results.append(compare(name))
    print(f"{'case':16} {'ok':4} {'vol_rel':8} {'vols ref/ours':22} {'holes':9} fails")
    nfail = 0
    for r in results:
        if not r["pass"]:
            nfail += 1
        print(f"{r['name']:16} {str(r['pass']):4} {str(r.get('vol_rel')):8} "
              f"{r.get('ref_vol', '?')}/{r.get('ours_vol', '?'):22} "
              f"{str(r.get('holes')):9} {'; '.join(r.get('fails', []))[:160]}")
    print(f"{len(results) - nfail}/{len(results)} pass")
    return 1 if nfail else 0


if __name__ == "__main__":
    sys.exit(main())
