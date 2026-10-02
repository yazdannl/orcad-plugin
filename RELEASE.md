# Releasing orcad

A release is one commit on `main` that passes every check, plus a `v*` tag.
Pushing the tag makes GitHub Actions verify the tag on Linux, Windows and macOS
and then publish the GitHub release with the bundled `orcad.py` attached.

## 1. Prepare

```sh
cd frontend && npm ci && cd ..
python3 packaging/bundle.py --release   # build frontend, embed into orcad.py, run every test
python3 packaging/bundle.py --check     # must report nothing stale
```

`--release` runs `python3 -m pytest -q tests` and `npm test`. The OpenSCAD
render tests skip unless a 2023+ `openscad` is on `PATH`.

## 2. Bump the version

The version appears in four places and `tests/test_bundle.py::test_versions_agree`
enforces that the first three match:

| File | Field |
|---|---|
| `orcad.py` | `# version = "…"` in the PEP 723 block **and** `PLUGIN_VERSION = "…"` |
| `frontend/package.json` | `"version"` (mirror it in `frontend/package-lock.json`) |
| `README.md` | version in the title |

Then add a `## <version> — <title>` section at the top of `CHANGELOG.md`.
`python3 packaging/release_notes.py v<version>` prints that section and exits
non-zero when it is missing, so a tag without notes fails the release instead of
publishing an empty description.

Rebuild after every change to `frontend/`, `openscad/`, `ai/` or the version
header: `python3 packaging/bundle.py`. `--check` in CI compares the committed
`orcad.py` against a fresh build, so a forgotten rebuild fails the tag.

## 3. Commit, tag, push

```sh
git add -A
git commit -m "Release v0.9.4"
git push origin main
git tag -a v0.9.4 -m "v0.9.4"
git push origin v0.9.4
```

The `release` job only runs for tags whose name starts with `v`, and only after
the `verify` matrix passes. Watch it at
<https://github.com/yazdannl/orcad-plugin/actions>.

## 4. Check the release

`https://github.com/yazdannl/orcad-plugin/releases/tag/v0.9.4` should list
`orcad.py` (about 440 KB) with the CHANGELOG section as notes. Download that
file and install it with **Plugins ▾ Install local plugin**, or drop it into
`<Orca data dir>/orca_plugins/orcad/orcad.py`; OrcaSlicer 2.4.2 or newer
(including Nightly) is required, and the plugin must be activated in the Plugins
dialog.

## Notes

- Tag order matters: push `main` first, so the tag cannot point at an unpushed
  commit.
- Rolling a tag back is a new patch release, not a moved tag.
- There is no changelog or version handling inside the plugin itself beyond the
  metadata above; the Plugins dialog reads it from there.
