"""Per-user OpenSCAD provisioning with verified downloads (no network access)."""
from __future__ import annotations

import hashlib
import io
import re
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


class FakeDiskImages:
    """An in-memory mount table plus fake hdiutil/ditto, all files under tmp_path."""

    def __init__(self, tmp_path, *, detach_clears=True, observe=None):
        self.tmp_path = tmp_path
        self.detach_clears = detach_clears
        self.observe = observe or (lambda _event, _path: None)
        self.mounts = set()
        self.commands = []

    def mount(self, path):
        path.mkdir(parents=True, exist_ok=True)
        self.mounts.add(path)

    def is_mount(self, path):
        return path in self.mounts

    def run_quiet(self, argv, cwd=None, *, allow_failure=False):
        self.commands.append(list(argv))
        tool = Path(argv[0]).name
        action = argv[1] if tool == "hdiutil" else None
        if tool == "hdiutil" and action == "attach":
            mount = Path(argv[argv.index("-mountpoint") + 1])
            self.observe("attach", mount)
            (mount / "OpenSCAD.app").mkdir(parents=True, exist_ok=True)
            self.mount(mount)
        elif tool == "hdiutil" and action == "detach":
            path = Path(argv[2])
            self.observe("detach", path)
            clears = self.detach_clears(path) if callable(self.detach_clears) else self.detach_clears
            if clears:
                self.mounts.discard(path)
        elif tool == "ditto":
            target = Path(argv[-1])
            self.observe("ditto", target)
            binary = target / "Contents" / "MacOS" / "OpenSCAD"
            binary.parent.mkdir(parents=True, exist_ok=True)
            binary.write_bytes(b"fake OpenSCAD")
        else:
            raise AssertionError(f"unexpected setup command: {argv}")


def fake_dmg_setup(monkeypatch, fake, tmp_path, payload=b"fake dmg"):
    monkeypatch.setattr(bootstrap, "_is_macos", lambda: True)
    monkeypatch.setattr(bootstrap, "_is_mount", fake.is_mount)
    monkeypatch.setattr(bootstrap, "_run_quiet", fake.run_quiet)
    artifact = use_artifact(monkeypatch, ("darwin", "universal"),
                            sha256=hashlib.sha256(payload).hexdigest())
    return artifact, Server({artifact.url: payload})


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
    aarch64 = bootstrap.ARTIFACTS[("linux", "aarch64")]
    assert aarch64.pattern and re.search(aarch64.pattern, aarch64.filename)
    with pytest.raises(BackendError) as info:
        bootstrap.artifact_for("freebsd", "x86_64")
    assert info.value.code == ErrorCode.UNSUPPORTED_PLATFORM


@posix_exec_only
def test_zip_install_is_verified_and_reused_without_network(tmp_path, monkeypatch, no_system_openscad):
    payload = zip_with({"OpenSCAD/openscad.exe": version_script()})
    artifact = use_artifact(monkeypatch, ("win32", "x86_64"), sha256=hashlib.sha256(payload).hexdigest())
    server = Server({artifact.url: payload})
    path, version = bootstrap.ensure_openscad(root=tmp_path, system="win32", machine="x86_64", opener=server)
    assert path.name == "openscad.exe" and path.is_relative_to(tmp_path / "win32-x86_64")
    assert version == artifact.version, "the pinned version is recorded without starting the download"
    assert not (tmp_path / artifact.filename).exists()  # the archive is removed after install
    again = bootstrap.ensure_openscad(root=tmp_path, system="win32", machine="x86_64", opener=server)
    assert again == (path, version) and len(server.requests) == 1


@posix_exec_only
def test_zip_install_skips_audit_denied_config_files(tmp_path, monkeypatch, no_system_openscad):
    """OrcaSlicer denies these names even under data_dir(), including on Windows."""
    payload = zip_with({
        "OpenSCAD/bin/openscad.exe": version_script(),
        "OpenSCAD/fonts/05-osx-fonts.conf": b"alias macOS font directory",
        "OpenSCAD/fonts/10-liberation.conf": b"alias Liberation fonts",
        "OpenSCAD/settings/openscad.ini": b"[general]",
        "OpenSCAD/CONF/notes.txt": b"a denied path component",
    })
    artifact = use_artifact(monkeypatch, ("win32", "x86_64"), sha256=hashlib.sha256(payload).hexdigest())
    path, version = bootstrap.ensure_openscad(root=tmp_path, system="win32", machine="x86_64",
                                              opener=Server({artifact.url: payload}))
    destination = tmp_path / "win32-x86_64"
    assert path.is_file() and path.name == "openscad.exe" and version == artifact.version
    assert (destination / "orcad-install.json").is_file()
    for name in ("OpenSCAD/fonts/05-osx-fonts.conf", "OpenSCAD/fonts/10-liberation.conf",
                 "OpenSCAD/settings/openscad.ini", "OpenSCAD/CONF/notes.txt"):
        assert not (destination / name).exists()


@posix_exec_only
def test_install_never_starts_the_downloaded_binary(tmp_path, monkeypatch, no_system_openscad):
    """One audited command line: installing must not run a `--version` probe.

    OrcaSlicer asks the user about each distinct command a plugin starts, so a
    probe during setup would be a second prompt before the first render.
    """
    marker = tmp_path / "ran"
    script = (f"#!{sys.executable}\nimport pathlib\npathlib.Path({str(marker)!r}).write_text('x')\n"
              "print('OpenSCAD version 2026.09.27')\n").encode()
    payload = zip_with({"OpenSCAD/openscad.exe": script})
    artifact = use_artifact(monkeypatch, ("win32", "x86_64"), sha256=hashlib.sha256(payload).hexdigest())
    path, version = bootstrap.ensure_openscad(root=tmp_path, system="win32", machine="x86_64",
                                              opener=Server({artifact.url: payload}))
    assert path.is_file() and version == artifact.version
    assert not marker.exists(), "the downloaded OpenSCAD was executed during setup"


def test_checksum_mismatch_installs_nothing(tmp_path, monkeypatch, no_system_openscad):
    artifact = use_artifact(monkeypatch, ("win32", "x86_64"), sha256="0" * 64)
    server = Server({artifact.url: zip_with({"openscad.exe": version_script()})})
    with pytest.raises(BackendError) as info:
        bootstrap.ensure_openscad(root=tmp_path, system="win32", machine="x86_64", opener=server)
    assert info.value.code == ErrorCode.INSTALL_FAILED and "checksum" in info.value.details["cause"]
    assert "setup failed:" in info.value.message and "checksum" in info.value.message
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
    path, version = bootstrap.ensure_openscad(root=tmp_path, system="win32", machine="x86_64", opener=server)
    assert path.is_file() and version == "2026.10.02"
    assert server.requests[-1] == bootstrap.SNAPSHOT_INDEX + newest


@posix_exec_only
def test_a_dropped_connection_is_retried_once(tmp_path, monkeypatch, no_system_openscad):
    payload = zip_with({"openscad.exe": version_script()})
    artifact = use_artifact(monkeypatch, ("win32", "x86_64"), sha256=hashlib.sha256(payload).hexdigest())
    attempts = []

    class Flaky:
        def __init__(self):
            self.requests = []

        def __call__(self, request, timeout):
            self.requests.append(request.full_url)
            if len(self.requests) == 1:
                raise urllib.error.URLError("connection reset")
            return Response(payload, request.full_url)

    opener = Flaky()
    path, version = bootstrap.ensure_openscad(root=tmp_path, system="win32", machine="x86_64", opener=opener)
    assert path.is_file() and version == artifact.version
    assert opener.requests == [artifact.url, artifact.url]


@posix_exec_only
def test_appimage_is_extracted_so_fuse_is_not_needed(tmp_path, monkeypatch, no_system_openscad):
    inner = version_script().decode()
    appimage = (f"#!{sys.executable}\nimport os, pathlib, sys\n"
                "if '--appimage-extract' in sys.argv:\n"
                "    target = pathlib.Path('squashfs-root/usr/bin'); target.mkdir(parents=True)\n"
                f"    (target / 'openscad').write_text({inner!r}); raise SystemExit(0)\n"
                "raise SystemExit('FUSE is not available')\n").encode()
    artifact = use_artifact(monkeypatch, ("linux", "x86_64"), sha256=hashlib.sha256(appimage).hexdigest())
    path, _version = bootstrap.ensure_openscad(root=tmp_path, system="linux", machine="x86_64",
                                               opener=Server({artifact.url: appimage}))
    assert path.parts[-4:] == ("squashfs-root", "usr", "bin", "openscad")
    assert path.stat().st_mode & stat.S_IXUSR
    assert not (path.parents[3] / artifact.filename).exists()


def test_archive_paths_cannot_escape(tmp_path):
    archive = tmp_path / "bad.zip"
    archive.write_bytes(zip_with({"../escape.exe": b"x"}))
    with pytest.raises(ValueError, match="unsafe"):
        bootstrap._safe_extract_zip(archive, tmp_path / "out")


def test_failed_setup_writes_a_diagnostic(tmp_path, monkeypatch):
    def fake_ensure(root=None):
        raise BackendError(ErrorCode.INSTALL_FAILED, "offline")

    monkeypatch.setattr(bootstrap, "ensure_openscad", fake_ensure)
    monkeypatch.setattr(bootstrap, "_STATE", {"state": "idle", "path": None, "version": None, "error": None,
                                              "progress": None})
    with pytest.raises(BackendError, match="offline"):
        bootstrap.wait_for_openscad(timeout=5, root=tmp_path)
    assert "offline" in (tmp_path / "setup-error.txt").read_text(encoding="utf-8")


def test_macos_stale_mount_is_detached_before_the_install_folder_is_cleared(
        tmp_path, monkeypatch, no_system_openscad):
    legacy_mount = tmp_path / "darwin-universal" / "mnt"
    sentinel = legacy_mount / "stale.txt"
    observations = []

    def observe(event, path):
        observations.append((event, path, sentinel.exists()))

    fake = FakeDiskImages(tmp_path, observe=observe)
    artifact, server = fake_dmg_setup(monkeypatch, fake, tmp_path)
    legacy_mount.mkdir(parents=True)
    (legacy_mount / "OpenSCAD.app").mkdir()
    sentinel.write_text("left mounted by the previous setup")
    fake.mount(legacy_mount)

    executable, version = bootstrap.ensure_openscad(
        root=tmp_path, system="darwin", machine="arm64", opener=server)

    destination = tmp_path / "darwin-universal"
    assert executable == destination / "OpenSCAD.app" / "Contents" / "MacOS" / "OpenSCAD"
    assert executable.is_file() and version == artifact.version
    assert (destination / "orcad-install.json").is_file()
    detached = ("detach", legacy_mount, True)
    attached = ("attach", destination.parent / "mnt", False)
    assert detached in observations, "the stale volume must be detached before rmtree reaches it"
    assert attached in observations, "the fake attach must see an already cleared install folder"
    assert observations.index(detached) < observations.index(attached)
    assert not fake.is_mount(legacy_mount) and not fake.is_mount(destination.parent / "mnt")
    assert not sentinel.exists()


def test_macos_mount_that_never_clears_reports_its_path_and_detach_remedy(
        tmp_path, monkeypatch, no_system_openscad):
    fake = FakeDiskImages(tmp_path, detach_clears=False)
    artifact, server = fake_dmg_setup(monkeypatch, fake, tmp_path)
    monkeypatch.setattr(bootstrap, "_MOUNT_DETACH_TIMEOUT", 0.05)
    monkeypatch.setattr(bootstrap, "_MOUNT_DETACH_POLL", 0.01)
    mount = tmp_path / "darwin-universal" / "mnt"
    fake.mount(mount)

    with pytest.raises(BackendError) as info:
        bootstrap.ensure_openscad(root=tmp_path, system="darwin", machine="arm64", opener=server)

    message = f"{info.value.message} {info.value.details}"
    assert info.value.code == ErrorCode.INSTALL_FAILED
    assert str(mount) in message
    assert "hdiutil detach" in message
    assert ["/usr/bin/hdiutil", "detach", str(mount), "-force"] in fake.commands
    assert not any(command[:2] == ["/usr/bin/hdiutil", "attach"] for command in fake.commands)


def test_macos_dmg_attaches_beside_the_install_folder_not_inside_it(
        tmp_path, monkeypatch, no_system_openscad):
    fake = FakeDiskImages(tmp_path)
    _artifact, server = fake_dmg_setup(monkeypatch, fake, tmp_path)

    bootstrap.ensure_openscad(root=tmp_path, system="darwin", machine="arm64", opener=server)

    attach = next(command for command in fake.commands
                  if command[:2] == ["/usr/bin/hdiutil", "attach"])
    assert attach[attach.index("-mountpoint") + 1] == str(tmp_path / "mnt")
    assert str(tmp_path / "darwin-universal" / "mnt") not in attach


def test_macos_cleanup_detaches_current_mount_even_if_a_legacy_volume_is_stuck(
        tmp_path, monkeypatch, no_system_openscad):
    legacy = tmp_path / "darwin-universal" / "mnt"
    current = tmp_path / "mnt"
    fake = FakeDiskImages(tmp_path, detach_clears=lambda path: path != legacy)

    def observe(event, path):
        if event == "attach":
            fake.mount(legacy)  # a leftover volume reappears while this image is mounted

    fake.observe = observe
    monkeypatch.setattr(bootstrap, "_MOUNT_DETACH_TIMEOUT", 0.05)
    monkeypatch.setattr(bootstrap, "_MOUNT_DETACH_POLL", 0.01)
    _artifact, server = fake_dmg_setup(monkeypatch, fake, tmp_path)

    bootstrap.ensure_openscad(root=tmp_path, system="darwin", machine="arm64", opener=server)

    assert ["/usr/bin/hdiutil", "detach", str(current), "-force"] in fake.commands
    assert ["/usr/bin/hdiutil", "detach", str(legacy), "-force"] in fake.commands
    assert not fake.is_mount(current), "cleanup must never leave the current image attached"
    assert fake.is_mount(legacy)


@pytest.mark.parametrize("macos, expected", [
    (True, ["/usr/bin/hdiutil", "/usr/bin/ditto"]),
    (False, []),
])
def test_setup_processes_declares_dmg_tools_only_on_macos(monkeypatch, macos, expected):
    monkeypatch.setattr(bootstrap, "_is_macos", lambda: macos)
    assert bootstrap.DMG_TOOLS == ("/usr/bin/hdiutil", "/usr/bin/ditto")
    assert bootstrap.setup_processes() == expected


def test_background_state_reports_failures_and_retries(tmp_path, monkeypatch):
    attempts = []

    def fake_ensure(root=None):
        attempts.append(root)
        if len(attempts) == 1:
            raise BackendError(ErrorCode.INSTALL_FAILED, "offline")
        return Path("/opt/openscad"), "2026.09.22"

    monkeypatch.setattr(bootstrap, "ensure_openscad", fake_ensure)
    monkeypatch.setattr(bootstrap, "_STATE", {"state": "idle", "path": None, "version": None, "error": None,
                                              "progress": None})
    with pytest.raises(BackendError, match="offline"):
        bootstrap.wait_for_openscad(timeout=5)
    assert bootstrap.openscad_bootstrap_status()["state"] == "failed"
    assert bootstrap.wait_for_openscad(timeout=5) == Path("/opt/openscad")
    status = bootstrap.openscad_bootstrap_status()
    assert status["state"] == "ready" and status["version"] == "2026.09.22" and len(attempts) == 2
