"""Shared setup for optional build123d geometry tests.

The normal test suite intentionally does not import build123d.  Geometry tests
opt into the project's virtualenv so a system Python can still run pure tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


def project_site_packages() -> Path | None:
    """Return the project venv site-packages directory, if it exists."""
    matches = sorted((ROOT / ".venv" / "lib").glob("python*/site-packages"))
    return matches[-1] if matches else None


def require_project_build123d():
    """Import build123d from .venv or skip with setup instructions."""
    site_packages = project_site_packages()
    if site_packages is None:
        pytest.skip(
            "build123d integration tests skipped: project .venv is missing. "
            "Create it and install the optional CAD dependencies with "
            "`.venv/bin/python -m pip install build123d numpy`.",
            allow_module_level=True,
        )
    if str(site_packages) not in sys.path:
        sys.path.insert(0, str(site_packages))
    try:
        import build123d  # type: ignore[import-not-found]
    except Exception as exc:  # pragma: no cover - depends on local OCP install
        pytest.skip(
            "build123d integration tests skipped: project .venv cannot import "
            f"build123d/OCP ({type(exc).__name__}). Install or repair the "
            "optional CAD dependencies with `.venv/bin/python -m pip install "
            "build123d numpy`, then rerun `python3 verify/geometry.py`.",
            allow_module_level=True,
        )
    return build123d
