# Releasing orcad

A release is one commit on `main` that passes every check, plus a `v*` tag.
Pushing the tag makes GitHub Actions verify the tag on Linux, Windows and macOS
and then publish the GitHub release with the bundled `orcad.py` attached.

## 1. Prepare

```sh
cd frontend && npm ci && cd ..
python3 packaging/bundle.py --release   # build frontend, embed into orcad.py, run every test
python3 packaging/check.py              # the CI gate: pytest, then the bundle freshness check
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

A release is published only if the whole matrix is green, so run
`python3 packaging/check.py` (pytest plus the bundle check) before tagging.

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

## Orca Cloud

The plugin is published in the [Orca Cloud](https://cloud.orcaslicer.com)
Plugin Hub. A one-time setup in the web UI is required: in **Plugins → Shared
Plugins → Edit plugin → GitHub publishing**, connect `yazdannl/orcad-plugin`.
Nothing else is stored: each release authenticates with a GitHub-signed OIDC
token, so there is no secret in this repository.

After that, tagging a release publishes both places:

- `ci.yml`'s `release` job creates the GitHub release and then runs
  `packaging/publish_orcacloud.sh`, which uploads `orcad_any.py` to
  `https://api.orcaslicer.com/api/v1/plugin-publish/releases`.
- `publish-orcacloud.yml` does the same for a release published from the GitHub
  UI. It cannot cover tag-push releases, because GitHub does not start workflow
  runs for events created with the built-in `GITHUB_TOKEN`.

The script refuses a tag that disagrees with the plugin's own `version`, and
fails with a readable reason otherwise: HTTP 401 means the repository is not
connected to one of your plugins, and a version error means the tag is not
higher than the published one. Orca Cloud requires the uploaded filename to end
in a supported target OS/arch suffix, hence the `orcad_any.py` copy of
`orcad.py` (universal, pure Python).

## Notes

- Tag order matters: push `main` first, so the tag cannot point at an unpushed
  commit.
- A tag that never produced a release (CI failed, or the tag was a mistake) can
  be deleted and recreated: `git push --delete origin v0.9.4`. Once a release
  exists, never move a published tag; roll forward with a new patch version.
- There is no changelog or version handling inside the plugin itself beyond the
  metadata above; the Plugins dialog reads it from there.
