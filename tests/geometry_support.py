"""Shared setup for optional build123d geometry tests.

The normal test suite intentionally does not import build123d.  Geometry tests
opt into the project's virtualenv so a system Python can still run pure tests.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
PROJECT_VENV = ROOT / ".venv"


def project_site_packages() -> Path | None:
    """Return the project venv site-packages directory, if it exists."""
    candidates = [PROJECT_VENV / "Lib" / "site-packages"]
    candidates.extend(sorted((PROJECT_VENV / "lib").glob("python*/site-packages")))
    return next((path for path in candidates if path.is_dir()), None)


def _running_project_python() -> bool:
    """Keep the CAD suite from borrowing build123d from the test runner."""
    try:
        if Path(sys.prefix).resolve() == PROJECT_VENV.resolve():
            return True
    except OSError:
        pass
    # The project venv has no pytest dependency, so verify/geometry.py runs
    # system pytest with this exact site-packages directory on PYTHONPATH.
    site_packages = project_site_packages()
    return site_packages is not None and str(site_packages) in sys.path


def require_project_build123d():
    """Import build123d only from the project .venv, or skip with guidance."""
    if not _running_project_python():
        pytest.skip(
            "build123d integration tests are isolated from the lightweight test "
            "interpreter. Run `python3 verify/geometry.py` for the CAD suite.",
            allow_module_level=True,
        )
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
