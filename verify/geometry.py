#!/usr/bin/env python3
"""Run the optional build123d geometry regression suite.

Usage: python3 verify/geometry.py

The repository's .venv supplies build123d/OCP; pytest remains the lightweight
system test runner.  Missing CAD dependencies are an intentional, actionable
skip so pure tests never need build123d installed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = ROOT / ".venv" / "bin" / "python"


def _site_packages():
    matches = sorted((ROOT / ".venv" / "lib").glob("python*/site-packages"))
    return matches[-1] if matches else None


def main():
    if not VENV_PYTHON.is_file() or _site_packages() is None:
        print(
            "SKIP: geometry integration tests need the project .venv with "
            "build123d/OCP. Install with `.venv/bin/python -m pip install "
            "build123d numpy`, then rerun `python3 verify/geometry.py`.",
            flush=True,
        )
        return 0

    probe = subprocess.run(
        [str(VENV_PYTHON), "-c", "import build123d"],
        cwd=ROOT,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        timeout=30,
    )
    if probe.returncode:
        print(
            "SKIP: project .venv cannot import build123d/OCP. Repair the optional "
            "CAD dependencies with `.venv/bin/python -m pip install build123d "
            "numpy`, then rerun `python3 verify/geometry.py`.",
            flush=True,
        )
        return 0

    site_packages = _site_packages()
    env = os.environ.copy()
    env["PYTHONPATH"] = os.pathsep.join(
        str(path) for path in (site_packages, ROOT, env.get("PYTHONPATH", "")) if path
    )
    command = [
        sys.executable,
        "-m",
        "pytest",
        "tests/test_geometry_integration.py",
        "tests/test_baseplate_geometry.py",
        "tests/test_gridfinity_bin_geometry.py",
        "-q",
        "--disable-warnings",
    ]
    print("Running optional geometry suite with project .venv/build123d:", flush=True)
    print("  " + " ".join(command), flush=True)
    try:
        completed = subprocess.run(command, cwd=ROOT, env=env, timeout=300)
    except subprocess.TimeoutExpired:
        print(
            "ERROR: geometry suite exceeded 300 seconds. Retry the named test "
            "with `-vv` to identify the slow CAD build.",
            file=sys.stderr,
            flush=True,
        )
        return 124
    return completed.returncode


if __name__ == "__main__":
    raise SystemExit(main())
