#!/usr/bin/env python3
"""Render the pinned upstream reference for all 27 geometry cases.

OpenSCAD and the upstream checkout are intentionally explicit inputs.  No
machine-specific checkout path is assumed and an existing STL is used only
when its sidecar metadata still matches the source, revision, parameters,
tool version, options, and harness version.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
from pathlib import Path

try:
    from .matrix import (CASE_MATRIX, REFERENCE_OPTIONS, ROOT, atomic_write,
                         cache_valid, canonical, command_text,
                         make_reference_metadata, metadata_key, run_command,
                         selected_cases)
except ImportError:  # Running this file directly puts verify/ on sys.path.
    from matrix import (CASE_MATRIX, REFERENCE_OPTIONS, ROOT, atomic_write,
                        cache_valid, canonical, command_text,
                        make_reference_metadata, metadata_key, run_command,
                        selected_cases)

# Compatibility for scripts that imported the old reference-only mapping.
CASES = {name: case["reference"] for name, case in CASE_MATRIX.items()}
DEFAULT_CACHE = ROOT / ".verify-cache"
DEFAULT_TIMEOUT = 300.0


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--upstream", type=Path,
                        default=Path(os.environ["ORCAD_UPSTREAM"]) if os.environ.get("ORCAD_UPSTREAM") else None,
                        help="upstream OpenSCAD source checkout (or ORCAD_UPSTREAM)")
    parser.add_argument("--openscad",
                        default=os.environ.get("ORCAD_OPENSCAD"),
                        help="OpenSCAD executable (or ORCAD_OPENSCAD; PATH lookup is allowed)")
    parser.add_argument("--upstream-revision", default=os.environ.get("ORCAD_UPSTREAM_REVISION"),
                        help="expected upstream git HEAD; otherwise HEAD/fingerprint is recorded")
    parser.add_argument("--cache", type=Path,
                        default=Path(os.environ.get("ORCAD_VERIFY_CACHE", DEFAULT_CACHE)))
    parser.add_argument("--only", help="comma-separated case names")
    parser.add_argument("--timeout", type=float,
                        default=float(os.environ.get("ORCAD_SUBPROCESS_TIMEOUT", DEFAULT_TIMEOUT)))
    parser.add_argument("--force", action="store_true", help="rerender even when metadata matches")
    parser.add_argument("--dry-run", action="store_true", help="validate selection and print commands only")
    return parser.parse_args(argv)


def resolve_executable(value):
    if not value:
        value = shutil.which("openscad")
    else:
        if Path(value).parent == Path("."):
            value = shutil.which(value)
    if not value:
        raise SystemExit("OpenSCAD executable not found; supply --openscad PATH or ORCAD_OPENSCAD")
    path = Path(value).expanduser()
    if not path.is_file() or not os.access(path, os.X_OK):
        raise SystemExit(f"OpenSCAD executable is not executable: {path}")
    return path.resolve()


def validate_upstream(upstream):
    if not upstream.is_dir():
        raise SystemExit(f"upstream directory not found: {upstream}; supply --upstream PATH")
    entry = upstream / "gridfinity-rebuilt-bins.scad"
    if not entry.is_file():
        raise SystemExit(f"upstream entry file not found: {entry}; wrong checkout?")


def atomic_move(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    os.replace(source, destination)


def render_case(case, upstream, openscad, cache, timeout, force, upstream_revision):
    name = case["name"]
    stl = cache / f"ref_{name}.stl"
    sidecar = cache / f"ref_{name}.json"
    expected = make_reference_metadata(case, upstream, openscad, REFERENCE_OPTIONS,
                                       upstream_revision)
    if not force and cache_valid(stl, sidecar, expected):
        print(f"skip {name} (metadata matches)", flush=True)
        return True

    cache.mkdir(parents=True, exist_ok=True)
    driver = cache / f"driver_{name}.scad"
    temporary_stl = cache / f".ref_{name}.stl.tmp"
    command = [sys.executable, str(ROOT / "verify" / "w_scad.py"), "bins",
               canonical(case["reference"]), str(driver), "--root", str(upstream),
               "--fa", str(REFERENCE_OPTIONS["fa"]), "--fs", str(REFERENCE_OPTIONS["fs"])]
    completed = run_command(command, cwd=ROOT, timeout=timeout)
    if completed.returncode:
        raise RuntimeError(f"driver generation failed (exit {completed.returncode}): "
                           f"{command_text(command)}\n{completed.stderr[-1000:]}")

    render_command = [str(openscad), "-o", str(temporary_stl), str(driver)]
    print(f"render {name} ...", flush=True)
    completed = run_command(render_command, cwd=upstream, timeout=timeout)
    log = (completed.stdout + completed.stderr)
    atomic_write(cache / f"{name}.log", log)
    if completed.returncode or not temporary_stl.is_file():
        temporary_stl.unlink(missing_ok=True)
        raise RuntimeError(f"OpenSCAD failed for {name} (exit {completed.returncode}): "
                           f"{command_text(render_command)} (timeout {timeout}s)\n{log[-1000:]}")
    atomic_move(temporary_stl, stl)
    atomic_write(sidecar, json.dumps(
        {**expected, "cache_key": metadata_key(expected)}, indent=2) + "\n")
    print(f"done {name}", flush=True)
    return True


def main(argv=None):
    args = parse_args(argv)
    if args.timeout <= 0:
        raise SystemExit("--timeout must be greater than zero")
    cases = selected_cases(args.only)
    args.cache = args.cache.expanduser().resolve()
    if args.upstream:
        args.upstream = args.upstream.expanduser().resolve()
    if args.dry_run:
        print(f"dry-run: {len(cases)}/{len(CASE_MATRIX)} cases selected")
        for case in cases:
            print(f"render {case['name']}: openscad -o {args.cache}/ref_{case['name']}.stl <driver>")
        return 0
    if not args.upstream:
        raise SystemExit("upstream source is required; supply --upstream PATH or ORCAD_UPSTREAM")
    validate_upstream(args.upstream)
    openscad = resolve_executable(args.openscad)
    failures = []
    for case in cases:
        try:
            render_case(case, args.upstream, openscad, args.cache, args.timeout,
                        args.force, args.upstream_revision)
        except RuntimeError as exc:
            failures.append(f"{case['name']}: {exc}")
            print(f"ERROR: {failures[-1]}", file=sys.stderr, flush=True)
    if failures:
        print(f"{len(cases) - len(failures)}/{len(cases)} references ready")
        return 1
    print(f"{len(cases)}/{len(cases)} references ready")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (RuntimeError, ValueError) as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        raise SystemExit(2)
