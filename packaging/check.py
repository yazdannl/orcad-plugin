#!/usr/bin/env python3
"""Run the release gate: the test suite, then the bundle freshness check.

Both steps write to the job log, which is unreadable without repository access;
on failure this mirrors their last lines as GitHub Actions annotations, which
show up next to the failing step. Output is unchanged outside CI.

    python3 packaging/check.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAIL_LINES = 40


def run(title: str, command: list[str]) -> int:
    result = subprocess.run(command, cwd=ROOT, capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode and os.environ.get("GITHUB_ACTIONS"):
        lines = (result.stdout.splitlines() + result.stderr.splitlines())[-TAIL_LINES:]
        print(f"::error title={title}::" + "\n".join(lines).replace("\n", "%0A"))
    return result.returncode


def main() -> int:
    steps = [
        ("pytest", [sys.executable, "-m", "pytest", "-q", "--tb=short", "tests"]),
        ("bundle", [sys.executable, "packaging/bundle.py", "--check"]),
    ]
    for title, command in steps:
        if run(title, command):
            return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
