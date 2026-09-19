#!/usr/bin/env python3
"""Run orcad's generator end-to-end (generate -> export STL) with the venv.

Usage: w_b123d.py OBJECT '{"GX": 2, ...}' TOLERANCE OUT.stl [FORMAT]
Prints JSON with export stats. Uses orcad.generate_primitive_code +
orcad.run_build123d_code, i.e. the exact plugin code path.
"""
import json
import shutil
import sys

sys.path.insert(0, "/home/agent/OrcaCadPlugin")
import orcad

obj, params_json, tol, out = sys.argv[1], sys.argv[2], float(sys.argv[3]), sys.argv[4]
fmt = sys.argv[5] if len(sys.argv) > 5 else "stl"
code = orcad.generate_primitive_code(obj, json.loads(params_json))
res = orcad.run_build123d_code(code, fmt, tol, "verify")
shutil.move(res["file"], out)
print(json.dumps({"file": out, "stats": res["stats"], "size": res["size_bytes"]}))
