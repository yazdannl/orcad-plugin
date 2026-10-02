#!/usr/bin/env python3
"""Run the test suite and mirror failures as GitHub Actions annotations.

`python3 -m pytest` writes only to the job log, which is unreadable without
repository access; annotations show up next to the failing step instead. Output
is unchanged when run outside CI.

    python3 packaging/check.py
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TAIL_LINES = 60


def main() -> int:
    result = subprocess.run([sys.executable, "-m", "pytest", "-q", "--tb=short", "tests"],
                            cwd=ROOT, capture_output=True, text=True)
    sys.stdout.write(result.stdout)
    sys.stderr.write(result.stderr)
    if result.returncode and os.environ.get("GITHUB_ACTIONS"):
        lines = result.stdout.splitlines()
        for line in lines:
            if line.startswith(("FAILED ", "ERROR ")):
                print(f"::error title=pytest::{line}")
        print(f"::error title=pytest output::" + "\n".join(lines[-TAIL_LINES:]))
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
