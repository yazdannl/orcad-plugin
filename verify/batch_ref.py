#!/usr/bin/env python3
"""Batch-render reference STLs from entry-file drivers. Sequential, backgroundable.

Usage: python3 verify/batch_ref.py  (runs all cases; skips finished ones)
Each case: {name: {var: openscad-literal}}. Renders with the snapshot build.
"""
import json
import subprocess
import sys
from pathlib import Path

UPSTREAM = Path("/tmp/opencode/gridfinity-rebuilt-openscad")
SNAP = Path("/tmp/opencode/oscad-snap/squashfs-root/AppRun")
OUT = Path("/home/agent/OrcaCadPlugin/.verify-cache")
DRIVERS = UPSTREAM
VERIFY = Path("/home/agent/OrcaCadPlugin/verify")

BASE = {"gridx": 2, "gridy": 2, "gridz": 6, "divx": 1, "divy": 1,
        "style_tab": 5, "place_tab": 0, "scoop": 0,
        "refined_holes": "false", "magnet_holes": "false",
        "screw_holes": "false", "crush_ribs": "false",
        "chamfer_holes": "false", "printable_hole_top": "false",
        "only_corners": "false", "enable_thumbscrew": "false",
        "include_lip": "true"}

CASES = {
    "t_plain": {},
    "t_nolip": {"include_lip": "false"},
    "t_div": {"divx": 2, "divy": 2, "magnet_holes": "true"},
    "t_tabs_full": {"divx": 2, "divy": 2, "style_tab": 0},
    "t_tabs_center": {"divx": 2, "divy": 2, "style_tab": 3},
    "t_tabs_auto": {"divx": 3, "divy": 1, "gridx": 3, "style_tab": 1},
    "t_tabs_tl": {"divx": 2, "divy": 2, "style_tab": 1, "place_tab": 1},
    "t_scoop05": {"divx": 2, "divy": 2, "scoop": 0.5},
    "t_scoop1": {"divx": 2, "divy": 2, "scoop": 1},
    "t_cyl": {"divx": 2, "divy": 2, "cut_cylinders": "true", "cd": 10, "c_chamfer": 0.5},
    "t_depth": {"divx": 2, "divy": 2, "depth": 10},
    "t_magnet": {"magnet_holes": "true"},
    "t_screw": {"screw_holes": "true", "chamfer_holes": "true"},
    "t_screw_print": {"screw_holes": "true", "printable_hole_top": "true"},
    "t_refined": {"divx": 2, "divy": 2, "refined_holes": "true"},
    "t_crush": {"magnet_holes": "true", "crush_ribs": "true"},
    "t_chamfer": {"magnet_holes": "true", "chamfer_holes": "true"},
    "t_printable": {"magnet_holes": "true", "printable_hole_top": "true"},
    "t_corners": {"gridx": 3, "magnet_holes": "true", "only_corners": "true"},
    "t_thumbscrew": {"enable_thumbscrew": "true"},
    "t_default": {"gridx": 3, "style_tab": 1, "scoop": 1, "refined_holes": "true"},
    "t_hmode1": {"gridz_define": 1, "gridz": 35},
    "t_fill": {"height_internal": 10, "divx": 2, "divy": 2},
}


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    for name, over in CASES.items():
        stl = OUT / f"ref_{name}.stl"
        if stl.exists():
            print(f"skip {name} (done)", flush=True)
            continue
        params = dict(BASE)
        params.update(over)
        drv = DRIVERS / f"case_{name}.scad"
        subprocess.run([sys.executable, str(VERIFY / "w_scad.py"), "bins",
                        json.dumps(params), str(drv),
                        "--root", str(UPSTREAM)], check=True)
        print(f"render {name} ...", flush=True)
        r = subprocess.run([str(SNAP), "-o", str(stl), str(drv)],
                           capture_output=True, text=True, cwd=str(UPSTREAM))
        (OUT / f"{name}.log").write_text(r.stdout + r.stderr)
        if not stl.exists():
            print(f"FAILED {name}", flush=True)
        else:
            print(f"done {name}", flush=True)


if __name__ == "__main__":
    main()
