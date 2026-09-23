"""Tests for the verified, per-user OpenSCAD bootstrap."""
from __future__ import annotations

import hashlib
import io
import zipfile
from types import SimpleNamespace

import pytest

import openscad.bootstrap as bootstrap
from openscad.errors import BackendError, ErrorCode


class _Response:
    def __init__(self, payload: bytes, url: str):
        self._stream = io.BytesIO(payload)
        self.headers = {"Content-Length": str(len(payload))}
        self._url = url

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def geturl(self):
        return self._url

    def read(self, size=-1):
        return self._stream.read(size)


def test_supported_artifacts_are_https_and_pinned():
    assert set(bootstrap.ARTIFACTS) == {
        ("linux", "x86_64"), ("linux", "aarch64"), ("darwin", "universal"), ("win32", "x86_64")
    }
    for artifact in bootstrap.ARTIFACTS.values():
        assert artifact.url.startswith("https://files.openscad.org/")
        assert len(artifact.sha256) == 64
        assert int(artifact.version.split(".")[0]) >= 2023


def test_zip_extraction_rejects_traversal_and_symlinks(tmp_path):
    archive = tmp_path / "unsafe.zip"
    with zipfile.ZipFile(archive, "w") as stream:
        stream.writestr("../outside", b"bad")
    with pytest.raises(ValueError, match="unsafe path"):
        bootstrap._safe_extract_zip(archive, tmp_path / "out")

    safe_archive = tmp_path / "safe.zip"
    with zipfile.ZipFile(safe_archive, "w") as stream:
        stream.writestr("OpenSCAD/openscad.exe", b"binary")
    destination = tmp_path / "safe"
    bootstrap._safe_extract_zip(safe_archive, destination)
    assert bootstrap._find_executable(destination, "openscad.exe").read_bytes() == b"binary"


def test_ensure_downloads_checksum_verified_appimage_once(tmp_path, monkeypatch):
    payload = b"verified OpenSCAD appimage"
    artifact = bootstrap.OpenSCADArtifact(
        "test", "x86_64", "OpenSCAD-test.AppImage", "https://files.openscad.org/test",
        hashlib.sha256(payload).hexdigest(), "appimage", "OpenSCAD-test.AppImage", "2024.01",
    )
    monkeypatch.setitem(bootstrap.ARTIFACTS, ("test", "x86_64"), artifact)
    monkeypatch.setattr(
        bootstrap, "discover_openscad",
        lambda: (_ for _ in ()).throw(BackendError(ErrorCode.NOT_FOUND, "missing")),
    )
    monkeypatch.setattr(
        bootstrap, "probe_openscad",
        lambda path, timeout=None: SimpleNamespace(supported=True, warning=None),
    )
    calls = []

    def opener(request, timeout):
        calls.append((request.full_url, timeout))
        return _Response(payload, artifact.url)

    first = bootstrap.ensure_openscad(root=tmp_path / "cache", system="test", machine="x86_64", opener=opener)
    second = bootstrap.ensure_openscad(
        root=tmp_path / "cache", system="test", machine="x86_64",
        opener=lambda *args, **kwargs: pytest.fail("the verified cache should be reused"),
    )
    assert first == second
    assert first.is_file()
    assert calls == [(artifact.url, bootstrap.DOWNLOAD_TIMEOUT_SECONDS)]


def test_ensure_reports_checksum_failure_without_installing(tmp_path, monkeypatch):
    payload = b"not the pinned artifact"
    artifact = bootstrap.OpenSCADArtifact(
        "test", "x86_64", "OpenSCAD-test.AppImage", "https://files.openscad.org/test",
        "0" * 64, "appimage", "OpenSCAD-test.AppImage", "2024.01",
    )
    monkeypatch.setitem(bootstrap.ARTIFACTS, ("test", "x86_64"), artifact)
    monkeypatch.setattr(
        bootstrap, "discover_openscad",
        lambda: (_ for _ in ()).throw(BackendError(ErrorCode.NOT_FOUND, "missing")),
    )
    with pytest.raises(BackendError) as caught:
        bootstrap.ensure_openscad(
            root=tmp_path / "cache", system="test", machine="x86_64",
            opener=lambda *args, **kwargs: _Response(payload, artifact.url),
        )
    assert caught.value.code == ErrorCode.INSTALL_FAILED
    assert not list((tmp_path / "cache").glob("*.AppImage"))


def test_startup_bootstrap_is_non_blocking_and_waitable(tmp_path, monkeypatch):
    executable = tmp_path / "openscad"
    executable.write_text("fake", encoding="utf-8")
    monkeypatch.setattr(bootstrap, "ensure_openscad", lambda root=None: executable)
    with bootstrap._STATE_LOCK:
        bootstrap._STATE = "idle"
        bootstrap._STATE_PATH = None
        bootstrap._STATE_ERROR = None
        bootstrap._STATE_EVENT.clear()
    try:
        bootstrap.start_openscad_bootstrap(tmp_path)
        assert bootstrap.wait_for_openscad(timeout=2) == executable
        assert bootstrap.openscad_bootstrap_status()["state"] == "ready"
    finally:
        with bootstrap._STATE_LOCK:
            bootstrap._STATE = "idle"
            bootstrap._STATE_PATH = None
            bootstrap._STATE_ERROR = None
            bootstrap._STATE_EVENT.clear()


def test_unsupported_platform_has_a_manual_fallback_message():
    with pytest.raises(BackendError) as caught:
        bootstrap.artifact_for("plan9", "sparc")
    assert caught.value.code == ErrorCode.UNSUPPORTED_PLATFORM
    assert "install OpenSCAD >=2023 manually" in caught.value.message
