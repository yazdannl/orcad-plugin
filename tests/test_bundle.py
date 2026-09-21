"""Release pipeline guards; subprocesses and worktree writes stay mocked."""
import importlib.util
import subprocess
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("bundle_for_tests", ROOT / "packaging" / "bundle.py")
bundle = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(bundle)


def test_frontend_fingerprint_is_stable_and_detects_stale_artifacts(tmp_path):
    objects = bundle._load_objects()
    expected = bundle.spec_fingerprint(objects)
    region = bundle.build_frontend_region(objects)
    assert expected in region

    artifact = tmp_path / "index.html"
    artifact.write_text(f"<script>const fingerprint = '{expected}'</script>", encoding="utf-8")
    assert bundle.frontend_asset_matches(objects, artifact)
    artifact.write_text("<script>const fingerprint = '" + "0" * 64 + "'</script>", encoding="utf-8")
    assert not bundle.frontend_asset_matches(objects, artifact)


def test_status_paths_parses_porcelain_and_renames():
    result = SimpleNamespace(returncode=0, stdout=" M README.md\nR  old.py -> new.py\n?? scratch.txt\n")
    with mock.patch.object(bundle.subprocess, "run", return_value=result):
        assert bundle._status_paths() == frozenset({"README.md", "old.py", "new.py", "scratch.txt"})


def test_release_runs_stages_in_order_without_real_commands():
    events = []
    patches = {
        "_status_paths": mock.Mock(side_effect=[frozenset(), frozenset()]),
        "_load_objects": mock.Mock(return_value=["objects"]),
        "write_generated_specs": mock.Mock(side_effect=lambda objects: events.append("derive")),
        "_require_generated_frontend": mock.Mock(side_effect=lambda objects: events.append("guard source")),
        "_run_frontend_build": mock.Mock(side_effect=lambda: events.append("build")),
        "_require_frontend_artifact": mock.Mock(side_effect=lambda objects: events.append("guard artifact")),
        "write": mock.Mock(side_effect=lambda objects: events.append("embed")),
        "check": mock.Mock(return_value=True),
        "_run_stage": mock.Mock(side_effect=lambda label, command, cwd=bundle.ROOT: events.append(label)),
    }
    with mock.patch.multiple(bundle, **patches):
        bundle.release()

    assert events == [
        "derive", "guard source", "build", "guard artifact", "embed",
        "Python regression tests", "frontend regression tests", "diff whitespace check",
    ]


def test_release_rejects_unexpected_changes_created_by_pipeline():
    statuses = iter([frozenset(), frozenset({"unexpected.txt"})])
    events = []
    patches = {
        "_status_paths": mock.Mock(side_effect=statuses),
        "_load_objects": mock.Mock(return_value=["objects"]),
        "write_generated_specs": mock.Mock(),
        "_require_generated_frontend": mock.Mock(),
        "_run_frontend_build": mock.Mock(),
        "_require_frontend_artifact": mock.Mock(),
        "write": mock.Mock(),
        "check": mock.Mock(return_value=True),
        "_run_stage": mock.Mock(side_effect=lambda label, command, cwd=bundle.ROOT: events.append(label)),
    }
    with mock.patch.multiple(bundle, **patches):
        try:
            bundle.release()
        except RuntimeError as exc:
            assert "unexpected changes" in str(exc)
        else:
            raise AssertionError("unexpected pipeline output should fail")
    assert events[-1] == "diff whitespace check"


def test_check_only_does_not_write_or_embed():
    events = []
    patches = {
        "_status_paths": mock.Mock(side_effect=[frozenset(), frozenset()]),
        "_load_objects": mock.Mock(return_value=["objects"]),
        "_require_generated_frontend": mock.Mock(side_effect=lambda objects: events.append("guard source")),
        "_check_reproducible_frontend": mock.Mock(side_effect=lambda objects: events.append("reproducible build")),
        "_require_frontend_artifact": mock.Mock(side_effect=lambda objects: events.append("guard artifact")),
        "check": mock.Mock(return_value=True),
        "_run_stage": mock.Mock(side_effect=lambda label, command, cwd=bundle.ROOT: events.append(label)),
        "write": mock.Mock(),
        "write_generated_specs": mock.Mock(),
    }
    with mock.patch.multiple(bundle, **patches):
        bundle.release(check_only=True)
    patches["write"].assert_not_called()
    patches["write_generated_specs"].assert_not_called()
    assert events[:3] == ["guard source", "reproducible build", "guard artifact"]


def test_run_stage_propagates_failure_with_actionable_label():
    result = SimpleNamespace(returncode=7)
    with mock.patch.object(bundle.subprocess, "run", return_value=result):
        try:
            bundle._run_stage("frontend build", ["fake-build"])
        except RuntimeError as exc:
            assert "frontend build failed with exit 7" in str(exc)
        else:
            raise AssertionError("failed command should propagate")
