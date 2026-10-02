"""Cross-platform fake executables for the test suite.

POSIX runs a shebang script directly. Windows cannot exec an extension-less
script, so there the fake is a .cmd wrapper around the same Python source.
"""
from __future__ import annotations

import os
import stat
import sys
from pathlib import Path

import pytest

WINDOWS = os.name == "nt"

#: Windows synthesizes file modes, so mode assertions are meaningless there.
posix_modes_only = pytest.mark.skipif(WINDOWS, reason="POSIX file modes only")

#: Tests whose fake binaries are shebang scripts cannot run on Windows.
posix_exec_only = pytest.mark.skipif(WINDOWS, reason="fake binaries are shebang scripts")


def assert_mode(path: Path, mode: int) -> None:
    """Assert a POSIX file mode, skipping the check where modes are synthetic."""
    if not WINDOWS:
        assert stat.S_IMODE(path.stat().st_mode) == mode


def fake_command(directory: Path, name: str, source: str) -> Path:
    """Write `source` as an executable command named `name`; return the runnable path."""
    directory.mkdir(parents=True, exist_ok=True)
    if WINDOWS:
        script = directory / f"{name}.py"
        script.write_text(source, encoding="utf-8")
        wrapper = directory / f"{name}.cmd"
        wrapper.write_text(f'@echo off\n"{sys.executable}" "{script}" %*\n', encoding="utf-8")
        return wrapper
    path = directory / name
    path.write_text(f"#!{sys.executable}\n{source}", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path
