"""Per-user OpenSCAD provisioning with verified downloads (no network access)."""
from __future__ import annotations

import hashlib
import io
import stat
import sys
import urllib.error
import zipfile
from dataclasses import replace
from pathlib import Path

import pytest

import openscad.bootstrap as bootstrap
from fakes import posix_exec_only
from openscad.errors import BackendError, ErrorCode


def version_script(version="2026.09.27") -> bytes:
    return f"#!{sys.executable}\nprint('OpenSCAD version {version}')\n".encode()


def zip_with(files: dict[str, bytes]) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (0o755 if name.endswith(".exe") else 0o644) << 16
            archive.writestr(info, data)
    return buffer.getvalue()


class Response(io.BytesIO):
    def __init__(self, data: bytes, url: str):
        super().__init__(data)
        self.headers = {"Content-Length": str(len(data))}
        self.url = url

    def geturl(self):
        return self.url

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()


class Server:
    """Fake opener: url -> bytes, or an int HTTP status to raise."""

    def __init__(self, routes):
        self.routes = routes
        self.requests = []

    def __call__(self, request, timeout):
        url = request.full_url
        self.requests.append(url)
        body = self.routes.get(url, 404)
        if isinstance(body, int):
            raise urllib.error.HTTPError(url, body, "status", {}, None)
        return Response(body, url)


@pytest.fixture
def no_system_openscad(monkeypatch):
    def missing(*_args, **_kwargs):
        raise BackendError(ErrorCode.NOT_FOUND, "missing")
    monkeypatch.setattr(bootstrap, "discover_openscad", missing)


def use_artifact(monkeypatch, key, **changes):
    artifact = replace(bootstrap.ARTIFACTS[key], **changes)
    monkeypatch.setitem(bootstrap.ARTIFACTS, key, artifact)
    return artifact


def test_artifacts_are_pinned_https_snapshots():
    assert set(bootstrap.ARTIFACTS) == {("linux", "x86_64"), ("linux", "aarch64"), ("darwin", "universal"), ("win32", "x86_64")}
    for artifact in bootstrap.ARTIFACTS.values():
        assert artifact.url == bootstrap.SNAPSHOT_INDEX + artifact.filename
        assert len(artifact.sha256) == 64 and int(artifact.version[:4]) >= 2023
    assert bootstrap.artifact_for("win32", "AMD64").platform == "win32"
    assert bootstrap.artifact_for("darwin", "arm64").architecture == "universal"
    with pytest.raises(BackendError) as info:
        bootstrap.artifact_for("freebsd", "x86_64")
    assert info.value.code == ErrorCode.UNSUPPORTED_PLATFORM


@posix_exec_only
def test_zip_install_is_verified_and_reused_without_network(tmp_path, monkeypatch, no_system_openscad):
    payload = zip_with({"OpenSCAD/openscad.exe": version_script()})
    artifact = use_artifact(monkeypatch, ("win32", "x86_64"), sha256=hashlib.sha256(payload).hexdigest())
    server = Server({artifact.url: payload})
    path = bootstrap.ensure_openscad(root=tmp_path, system="win32", machine="x86_64", opener=server)
    assert path.name == "openscad.exe" and path.is_relative_to(tmp_path / "win32-x86_64")
    assert not (tmp_path / artifact.filename).exists()  # the archive is removed after install
    again = bootstrap.ensure_openscad(root=tmp_path, system="win32", machine="x86_64", opener=server)
    assert again == path and len(server.requests) == 1


def test_checksum_mismatch_installs_nothing(tmp_path, monkeypatch, no_system_openscad):
    artifact = use_artifact(monkeypatch, ("win32", "x86_64"), sha256="0" * 64)
    server = Server({artifact.url: zip_with({"openscad.exe": version_script()})})
    with pytest.raises(BackendError) as info:
        bootstrap.ensure_openscad(root=tmp_path, system="win32", machine="x86_64", opener=server)
    assert info.value.code == ErrorCode.INSTALL_FAILED and "checksum" in info.value.details["cause"]
    assert not (tmp_path / "win32-x86_64").exists()
    assert not list(tmp_path.glob("*.part"))


@posix_exec_only
def test_missing_pinned_snapshot_falls_back_to_the_newest_published_one(tmp_path, monkeypatch, no_system_openscad):
    payload = zip_with({"openscad.exe": version_script("2026.10.02")})
    artifact = bootstrap.ARTIFACTS[("win32", "x86_64")]
    newest = "OpenSCAD-2026.10.02-x86-64.zip"
    index = (f'<a href="OpenSCAD-2026.09.30-x86-64.zip">x</a><a href="{newest}">x</a>'
             f'<a href="{newest}.sha256">x</a><a href="OpenSCAD-2026.10.02-x86-64-Installer.exe">x</a>').encode()
    server = Server({artifact.url: 404, bootstrap.SNAPSHOT_INDEX: index,
                     bootstrap.SNAPSHOT_INDEX + newest: payload,
                     bootstrap.SNAPSHOT_INDEX + newest + ".sha256": f"{hashlib.sha256(payload).hexdigest()}  {newest}\n".encode()})
    path = bootstrap.ensure_openscad(root=tmp_path, system="win32", machine="x86_64", opener=server)
    assert path.is_file()
    assert server.requests[-1] == bootstrap.SNAPSHOT_INDEX + newest


@posix_exec_only
def test_appimage_is_extracted_so_fuse_is_not_needed(tmp_path, monkeypatch, no_system_openscad):
    inner = version_script().decode()
    appimage = (f"#!{sys.executable}\nimport os, pathlib, sys\n"
                "if '--appimage-extract' in sys.argv:\n"
                "    target = pathlib.Path('squashfs-root/usr/bin'); target.mkdir(parents=True)\n"
                f"    (target / 'openscad').write_text({inner!r}); raise SystemExit(0)\n"
                "raise SystemExit('FUSE is not available')\n").encode()
    artifact = use_artifact(monkeypatch, ("linux", "x86_64"), sha256=hashlib.sha256(appimage).hexdigest())
    path = bootstrap.ensure_openscad(root=tmp_path, system="linux", machine="x86_64",
                                     opener=Server({artifact.url: appimage}))
    assert path.parts[-4:] == ("squashfs-root", "usr", "bin", "openscad")
    assert path.stat().st_mode & stat.S_IXUSR
    assert not (path.parents[3] / artifact.filename).exists()


def test_archive_paths_cannot_escape(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(zip_with({"../escape.exe": b"x"}))
    with pytest.raises(ValueError, match="unsafe"):
        bootstrap._safe_extract_zip(archive, tmp_path / "out")


def test_background_state_reports_failures_and_retries(tmp_path, monkeypatch):
    attempts = []

    def fake_ensure(root=None):
        attempts.append(root)
        if len(attempts) == 1:
            raise BackendError(ErrorCode.INSTALL_FAILED, "offline")
        return Path("/opt/openscad")

    monkeypatch.setattr(bootstrap, "ensure_openscad", fake_ensure)
    monkeypatch.setattr(bootstrap, "_STATE", {"state": "idle", "path": None, "error": None, "progress": None})
    with pytest.raises(BackendError, match="offline"):
        bootstrap.wait_for_openscad(timeout=5)
    assert bootstrap.openscad_bootstrap_status()["state"] == "failed"
    assert bootstrap.wait_for_openscad(timeout=5) == Path("/opt/openscad")
    assert bootstrap.openscad_bootstrap_status()["state"] == "ready" and len(attempts) == 2
