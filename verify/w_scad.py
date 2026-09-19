#!/usr/bin/env python3
"""Generate an OpenSCAD driver from an entry file by overriding assignments.

Usage: w_scad.py bins|baseplate '{"gridx": 2, ...}' OUT.scad [--fa 4] [--fs 0.25]

Copies the entry file and replaces top-level `name = ...;` / `$fa` / `$fs`
assignments with the given values (OpenSCAD literals, e.g. numbers or
true/false). Derived lines (like hole_options = bundle_hole_options(...))
are left intact so they recompute from the overridden booleans.
"""
import json
import re
import sys
from pathlib import Path

UPSTREAM = Path("/tmp/opencode/gridfinity-rebuilt-openscad")
ENTRIES = {
    "bins": "gridfinity-rebuilt-bins.scad",
    "baseplate": "gridfinity-rebuilt-baseplate.scad",
}


def build_driver(entry, overrides, fa=4, fs=0.25, root=UPSTREAM):
    text = (root / ENTRIES[entry]).read_text()
    text = re.sub(r"^\$fa\s*=.*;$", f"$fa = {fa};", text, flags=re.M)
    text = re.sub(r"^\$fs\s*=.*;$", f"$fs = {fs};", text, flags=re.M)
    for name, value in overrides.items():
        pattern = re.compile(rf"^{re.escape(name)}\s*=.*?;", re.M)
        new_text, count = pattern.subn(f"{name} = {value};", text, count=1)
        if count != 1:
            raise ValueError(f"assignment for {name} not found exactly once")
        text = new_text
    # compat: OpenSCAD 2021.01 rejects trailing commas in calls (repo targets
    # dev snapshots); strip comma before closing paren (list commas stay).
    text = re.sub(r",(\s*\))", r"\1", text)
    return text


if __name__ == "__main__":
    entry, params_json, out = sys.argv[1], sys.argv[2], sys.argv[3]
    fa = float(sys.argv[sys.argv.index("--fa") + 1]) if "--fa" in sys.argv else 4
    fs = float(sys.argv[sys.argv.index("--fs") + 1]) if "--fs" in sys.argv else 0.25
    root = Path(sys.argv[sys.argv.index("--root") + 1]) if "--root" in sys.argv else UPSTREAM
    Path(out).write_text(build_driver(entry, json.loads(params_json), fa, fs, root))
    print(f"wrote {out}")
