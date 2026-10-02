#!/usr/bin/env python3
"""Print the CHANGELOG.md section for one version, used as GitHub release notes.

    python3 packaging/release_notes.py v0.9.4

Exits non-zero when the version has no section, so a tag without changelog
entries fails the release instead of publishing empty notes.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parents[1] / "CHANGELOG.md"


def section(version: str) -> str:
    version = version.removeprefix("v")
    text = CHANGELOG.read_text(encoding="utf-8")
    match = re.search(rf"^## +{re.escape(version)}\b.*?(?=^## |\Z)", text, re.M | re.S)
    if not match:
        raise SystemExit(f"CHANGELOG.md has no section for {version}")
    return match.group(0).strip()


def main() -> int:
    if len(sys.argv) != 2:
        print(f"usage: {Path(sys.argv[0]).name} <version>", file=sys.stderr)
        return 2
    print(section(sys.argv[1]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
