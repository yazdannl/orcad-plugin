#!/usr/bin/env python3
"""Build and compare the complete, reproducible geometry matrix.

Usage examples::

    python3 verify/matrix.py --cache .verify-cache --only t_plain,t_div
    python3 verify/matrix.py --help

Reference STLs are produced by ``batch_ref.py``.  Each generated STL has a
JSON sidecar; a sidecar whose source, case parameters, tool version, or harness
version no longer matches is rejected instead of being used silently.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CACHE = ROOT / ".verify-cache"
HARNESS_VERSION = "geometry-verify-24.1"
DEFAULT_TIMEOUT = 300.0
REFERENCE_OPTIONS = {"fa": 4, "fs": 0.25}

BASE = {
    "GX": 2, "GY": 2, "HU": 6, "HMODE": 0, "ZS": False, "FILL": 0,
    "WALL": 0.95, "DX": 1, "DY": 1, "DEPTH": 0, "SCOOPW": 0.0,
    "TABSTYLE": 5, "TABPLACE": 0, "CYL": False, "CD": 10, "CCHAM": 0.5,
    "REFINED": False, "MAGNETS": False, "SCREW": False, "CRUSH": False,
    "CHAMFER": False, "PRINTABLE": False, "CORNERS": False,
    "THUMB": False, "LIP": True,
}

REF_BASE = {
    "gridx": 2, "gridy": 2, "gridz": 6, "divx": 1, "divy": 1,
    "style_tab": 5, "place_tab": 0, "scoop": 0,
    "refined_holes": "false", "magnet_holes": "false",
    "screw_holes": "false", "crush_ribs": "false",
    "chamfer_holes": "false", "printable_hole_top": "false",
    "only_corners": "false", "enable_thumbscrew": "false",
    "include_lip": "true",
}

# This is the one authoritative matrix.  Both sides are deliberately kept in
# the same row: adding a case cannot update comparison without updating
# reference generation as well.
_CASE_ROWS = [
    ("t_plain", {}, {}, ()),
    ("t_nolip", {"LIP": False}, {"include_lip": "false"}, ()),
    ("t_div", {"DX": 2, "DY": 2, "MAGNETS": True}, {"divx": 2, "divy": 2, "magnet_holes": "true"}, ("magnet_holes",)),
    ("t_tabs_full", {"DX": 2, "DY": 2, "TABSTYLE": 0}, {"divx": 2, "divy": 2, "style_tab": 0}, ("tabs",)),
    ("t_tabs_center", {"DX": 2, "DY": 2, "TABSTYLE": 3}, {"divx": 2, "divy": 2, "style_tab": 3}, ("tabs",)),
    ("t_tabs_auto", {"GX": 3, "DX": 3, "DY": 1, "TABSTYLE": 1}, {"gridx": 3, "divx": 3, "divy": 1, "style_tab": 1}, ("tabs",)),
    ("t_tabs_tl", {"DX": 2, "DY": 2, "TABSTYLE": 1, "TABPLACE": 1}, {"divx": 2, "divy": 2, "style_tab": 1, "place_tab": 1}, ("tabs",)),
    ("t_scoop05", {"DX": 2, "DY": 2, "SCOOPW": 0.5}, {"divx": 2, "divy": 2, "scoop": 0.5}, ("scoop",)),
    ("t_scoop1", {"DX": 2, "DY": 2, "SCOOPW": 1.0}, {"divx": 2, "divy": 2, "scoop": 1}, ("scoop",)),
    ("t_cyl", {"DX": 2, "DY": 2, "CYL": True}, {"divx": 2, "divy": 2, "cut_cylinders": "true", "cd": 10, "c_chamfer": 0.5}, ("cylinders",)),
    ("t_depth", {"DX": 2, "DY": 2, "DEPTH": 10}, {"divx": 2, "divy": 2, "depth": 10}, ("depth",)),
    ("t_magnet", {"MAGNETS": True}, {"magnet_holes": "true"}, ("magnet_holes",)),
    ("t_screw", {"SCREW": True, "CHAMFER": True}, {"screw_holes": "true", "chamfer_holes": "true"}, ("screw_holes",)),
    ("t_screw_print", {"SCREW": True, "PRINTABLE": True}, {"screw_holes": "true", "printable_hole_top": "true"}, ("screw_holes",)),
    ("t_refined", {"DX": 2, "DY": 2, "REFINED": True}, {"divx": 2, "divy": 2, "refined_holes": "true"}, ("refined_holes",)),
    ("t_crush", {"MAGNETS": True, "CRUSH": True}, {"magnet_holes": "true", "crush_ribs": "true"}, ("magnet_holes", "crush_ribs")),
    ("t_chamfer", {"MAGNETS": True, "CHAMFER": True}, {"magnet_holes": "true", "chamfer_holes": "true"}, ("magnet_holes",)),
    ("t_printable", {"MAGNETS": True, "PRINTABLE": True}, {"magnet_holes": "true", "printable_hole_top": "true"}, ("magnet_holes",)),
    ("t_corners", {"GX": 3, "MAGNETS": True, "CORNERS": True}, {"gridx": 3, "magnet_holes": "true", "only_corners": "true"}, ("corner_holes",)),
    ("t_thumbscrew", {"THUMB": True}, {"enable_thumbscrew": "true"}, ("thumbscrew",)),
    ("t_default", {"GX": 3, "TABSTYLE": 1, "SCOOPW": 1.0, "REFINED": True, "CRUSH": True, "CHAMFER": True, "PRINTABLE": True}, {"gridx": 3, "style_tab": 1, "scoop": 1, "refined_holes": "true"}, ("default_features",)),
    ("t_hmode1", {"HMODE": 1, "HU": 35}, {"gridz_define": 1, "gridz": 35}, ("height_mode",)),
    ("t_fill", {"DX": 2, "DY": 2, "FILL": 10}, {"height_internal": 10, "divx": 2, "divy": 2}, ("internal_fill",)),
    ("tabwide_auto", {"GX": 3, "GY": 1, "DX": 1, "DY": 1, "TABSTYLE": 1}, {"gridx": 3, "gridy": 1, "divx": 1, "divy": 1, "style_tab": 1}, ("tabs",)),
    ("tabwide_center", {"GX": 3, "GY": 1, "DX": 1, "DY": 1, "TABSTYLE": 3}, {"gridx": 3, "gridy": 1, "divx": 1, "divy": 1, "style_tab": 3}, ("tabs",)),
    ("tabwide_full", {"GX": 3, "GY": 1, "DX": 1, "DY": 1, "TABSTYLE": 0}, {"gridx": 3, "gridy": 1, "divx": 1, "divy": 1, "style_tab": 0}, ("tabs",)),
    ("newdef", {"TABSTYLE": 1, "SCOOPW": 1.0, "REFINED": True, "CRUSH": True, "CHAMFER": True, "PRINTABLE": True}, {"style_tab": 1, "scoop": 1, "refined_holes": "true", "crush_ribs": "true", "chamfer_holes": "true", "printable_hole_top": "true"}, ("default_features",)),
]

if len(_CASE_ROWS) != 27 or len({row[0] for row in _CASE_ROWS}) != 27:
    raise RuntimeError("geometry matrix must contain exactly 27 unique cases")


def _case(name, ours_overrides, reference_overrides, features):
    ours = dict(BASE)
    ours.update(ours_overrides)
    reference = dict(REF_BASE)
    reference.update(reference_overrides)
    return {"name": name, "ours": ours, "reference": reference,
            "features": list(features)}


CASE_MATRIX = {name: _case(name, ours, reference, features)
               for name, ours, reference, features in _CASE_ROWS}
# Compatibility for callers that used the old plugin-side mapping.
MATRIX = {name: case["ours"] for name, case in CASE_MATRIX.items()}
HEIGHTS = [0.2, 1, 2, 3, 4, 5, 10, 20, 30, 35, 40, 43]


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


def file_fingerprint(root, suffixes):
    """Hash source files without including machine paths or generated output."""
    digest = hashlib.sha256()
    files = sorted(p for suffix in suffixes for p in root.rglob(f"*{suffix}")
                   if p.is_file() and ".git" not in p.parts
                   and ".venv" not in p.parts and ".verify-cache" not in p.parts
                   and "node_modules" not in p.parts and "dist" not in p.parts)
    for path in files:
        rel = path.relative_to(root).as_posix()
        digest.update(rel.encode())
        digest.update(b"\0")
        digest.update(path.read_bytes())
        digest.update(b"\0")
    return digest.hexdigest()


def git_revision(root):
    try:
        result = subprocess.run(["git", "-C", str(root), "rev-parse", "HEAD"],
                                capture_output=True, text=True, timeout=10,
                                check=True)
    except (OSError, subprocess.SubprocessError):
        return None
    revision = result.stdout.strip()
    return revision or None


def source_identity(root, suffixes):
    revision = git_revision(root)
    return {"revision": revision,
            "fingerprint": file_fingerprint(root, suffixes)}


def tool_version(command, timeout=30):
    try:
        result = subprocess.run([str(command), "--version"], capture_output=True,
                                text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"tool version command timed out: {command} (timeout {timeout}s)") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot execute tool for version: {command}: {exc}") from exc
    if result.returncode:
        detail = (result.stdout or result.stderr).strip().splitlines()
        raise RuntimeError(f"tool version command failed: {command} (exit {result.returncode})"
                           + (f": {detail[0]}" if detail else ""))
    text = (result.stdout or result.stderr).strip().splitlines()
    version = text[0] if text else "unknown"
    return version


def metadata_key(metadata):
    body = dict(metadata)
    body.pop("cache_key", None)
    return hashlib.sha256(canonical(body).encode()).hexdigest()


def read_metadata(path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return data if isinstance(data, dict) else None


def cache_valid(stl, metadata_path, expected):
    actual = read_metadata(metadata_path)
    return stl.is_file() and actual == {**expected, "cache_key": metadata_key(expected)}


def atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent,
                                    prefix=f".{path.name}.", delete=False) as handle:
        handle.write(text)
        temporary = Path(handle.name)
    os.replace(temporary, path)


def command_text(command):
    return " ".join(subprocess.list2cmdline([str(part)]) if " " in str(part) else str(part)
                    for part in command)


def run_command(command, *, cwd, timeout):
    try:
        return subprocess.run(command, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(f"command timed out after {timeout}s: {command_text(command)} (cwd {cwd})") from exc
    except OSError as exc:
        raise RuntimeError(f"cannot execute command: {command_text(command)}: {exc}") from exc


def make_reference_metadata(case, upstream, openscad, options, upstream_revision=None):
    identity = source_identity(upstream, (".scad",))
    if upstream_revision and identity["revision"] != upstream_revision:
        raise RuntimeError("upstream revision mismatch: expected "
                           f"{upstream_revision}, found {identity['revision'] or 'not a git checkout'}")
    return {"schema": 1, "harness": HARNESS_VERSION,
            "harness_source": file_fingerprint(ROOT, (".py",)),
            "role": "reference", "case": case["name"],
            "params": case["reference"], "features": case["features"],
            "source": identity, "tool": {"version": tool_version(openscad)},
            "options": options}


def make_ours_metadata(case, python_command, options):
    return {"schema": 1, "harness": HARNESS_VERSION, "role": "ours",
            "case": case["name"], "params": case["ours"],
            "features": case["features"],
            "source": source_identity(ROOT, (".py",)),
            "tool": {"version": tool_version(python_command)},
            "options": options}


def parse_args(argv):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--only", help="comma-separated case names")
    parser.add_argument("--cache", type=Path,
                        default=Path(os.environ.get("ORCAD_VERIFY_CACHE", DEFAULT_CACHE)))
    parser.add_argument("--python", dest="python_command", type=Path,
                        default=Path(os.environ.get("ORCAD_PYTHON", ROOT / ".venv" / "bin" / "python")),
                        help="project Python used for build123d (default: .venv/bin/python)")
    parser.add_argument("--timeout", type=float,
                        default=float(os.environ.get("ORCAD_SUBPROCESS_TIMEOUT", DEFAULT_TIMEOUT)))
    parser.add_argument("--upstream", type=Path,
                        help="optional upstream checkout; validates reference source identity")
    parser.add_argument("--openscad",
                        default=os.environ.get("ORCAD_OPENSCAD"),
                        help="optional OpenSCAD executable for reference tool validation")
    parser.add_argument("--upstream-revision",
                        default=os.environ.get("ORCAD_UPSTREAM_REVISION"),
                        help="expected upstream git HEAD (also accepts ORCAD_UPSTREAM_REVISION)")
    parser.add_argument("--dry-run", action="store_true", help="print build/compare commands without running them")
    return parser.parse_args(argv)


def selected_cases(only):
    names = set(only.split(",")) if only else set(CASE_MATRIX)
    unknown = names - CASE_MATRIX.keys()
    if unknown:
        raise ValueError(f"unknown case(s): {', '.join(sorted(unknown))}")
    return [CASE_MATRIX[name] for name in CASE_MATRIX if name in names]


def main(argv=None):
    args = parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")
    cases = selected_cases(args.only)
    cache = args.cache.expanduser().resolve()
    args.python_command = args.python_command.expanduser().resolve()
    if args.dry_run:
        for case in cases:
            name = case["name"]
            print(f"build {name}: {args.python_command} verify/w_b123d.py gridfinity_bin <params> 0.05 {cache / ('ours_' + name + '.stl')}")
        print(f"dry-run: {len(cases)}/{len(CASE_MATRIX)} cases selected")
        return 0
    if not args.python_command.is_file():
        raise SystemExit(f"project Python not found: {args.python_command}; use --python or ORCAD_PYTHON")
    if args.upstream_revision and not args.upstream:
        raise SystemExit("--upstream-revision requires --upstream so it can be validated")
    if args.upstream:
        args.upstream = args.upstream.expanduser().resolve()
        if not args.upstream.is_dir():
            raise SystemExit(f"upstream directory not found: {args.upstream}")
        if args.upstream_revision:
            actual = git_revision(args.upstream)
            if actual != args.upstream_revision:
                raise SystemExit(f"upstream revision mismatch: expected {args.upstream_revision}, found {actual or 'not a git checkout'}")

    source_options = {"tolerance": 0.05, "compare_heights": HEIGHTS,
                      "tol_dim": 0.3, "tol_vol": 0.04,
                      "tol_hole_center": 0.4, "tol_hole_radius": 0.25,
                      "hole_z": 1.2}
    results = []
    for case in cases:
        name = case["name"]
        ours = cache / f"ours_{name}.stl"
        ours_meta = cache / f"ours_{name}.json"
        expected_ours = make_ours_metadata(case, args.python_command, source_options)
        if not cache_valid(ours, ours_meta, expected_ours):
            cache.mkdir(parents=True, exist_ok=True)
            command = [str(args.python_command), str(ROOT / "verify" / "w_b123d.py"),
                       "gridfinity_bin", canonical(case["ours"]), "0.05", str(ours)]
            print(f"build {name} ...", flush=True)
            completed = run_command(command, cwd=ROOT, timeout=args.timeout)
            if completed.returncode or not ours.is_file():
                detail = (completed.stdout + completed.stderr).strip()[-1000:]
                results.append({"name": name, "pass": False,
                                "fails": [f"build failed: {command_text(command)}"] + ([detail] if detail else [])})
                continue
            atomic_write(ours_meta, json.dumps({**expected_ours,
                                                "cache_key": metadata_key(expected_ours)}, indent=2) + "\n")
        ref = cache / f"ref_{name}.stl"
        ref_meta = cache / f"ref_{name}.json"
        if not ref.is_file() or not read_metadata(ref_meta):
            results.append({"name": name, "pass": False,
                            "fails": [f"reference cache missing/stale: {ref_meta}; run verify/batch_ref.py"]})
            continue
        actual_ref = read_metadata(ref_meta)
        if not actual_ref or actual_ref.get("cache_key") != metadata_key({k: v for k, v in actual_ref.items() if k != "cache_key"}):
            results.append({"name": name, "pass": False,
                            "fails": [f"reference cache metadata is invalid: {ref_meta}; rerun verify/batch_ref.py"]})
            continue
        expected_identity = {"schema": 1, "harness": HARNESS_VERSION,
                             "harness_source": file_fingerprint(ROOT, (".py",)),
                             "role": "reference", "case": name,
                             "params": case["reference"], "features": case["features"],
                             "options": REFERENCE_OPTIONS}
        if any(actual_ref.get(key) != value for key, value in expected_identity.items()):
            results.append({"name": name, "pass": False,
                            "fails": [f"reference cache identity is stale for {name}; rerun verify/batch_ref.py"]})
            continue
        if args.upstream:
            expected_source = source_identity(args.upstream, (".scad",))
            source_mismatch = actual_ref.get("source") != expected_source
            tool_mismatch = False
            if args.openscad:
                tool_mismatch = actual_ref.get("tool", {}).get("version") != tool_version(args.openscad)
            if args.upstream_revision and expected_source.get("revision") != args.upstream_revision:
                source_mismatch = True
            if source_mismatch or tool_mismatch:
                results.append({"name": name, "pass": False,
                                "fails": [f"reference cache is stale for {name}; rerun verify/batch_ref.py"]})
                continue
        command = [str(args.python_command), str(ROOT / "verify" / "w_compare.py"),
                   str(ref), str(ours), "--heights", ",".join(map(str, HEIGHTS)),
                   "--tol-dim", "0.3", "--tol-vol", "0.04",
                   "--tol-hole-center", "0.4", "--tol-hole-radius", "0.25"]
        command += ["--holes", "1.2"]
        completed = run_command(command, cwd=ROOT, timeout=args.timeout)
        try:
            data = json.loads(completed.stdout)
        except (TypeError, ValueError):
            data = {"pass": False, "fails": [f"comparison failed: {command_text(command)}",
                                                completed.stderr.strip()[-1000:]]}
        holes = data.get("holes", [{}])[0] if data.get("holes") else {}
        results.append({"name": name, "pass": bool(data.get("pass")) and completed.returncode == 0,
                        "fails": data.get("fails", []), "vol_rel": data.get("vol_rel_diff"),
                        "ref_vol": round(data.get("ref", {}).get("volume", 0)),
                        "ours_vol": round(data.get("ours", {}).get("volume", 0)),
                        "holes": (len(holes.get("ref", [])), len(holes.get("ours", [])))})

    print(f"{'case':16} {'ok':4} {'vol_rel':8} {'vols ref/ours':22} {'holes':9} fails")
    failures = [result for result in results if not result["pass"]]
    for result in results:
        print(f"{result['name']:16} {str(result['pass']):4} {str(result.get('vol_rel')):8} "
              f"{result.get('ref_vol', '?')}/{result.get('ours_vol', '?'):22} "
              f"{str(result.get('holes')):9} {'; '.join(result.get('fails', []))[:160]}")
    print(f"{len(results) - len(failures)}/{len(results)} pass")
    return 1 if failures else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        sys.exit(2)
