#!/usr/bin/env bash
# Publish plugin files to Orca Cloud from a GitHub Actions release run.
#
# Identity is a short-lived OIDC token signed by GitHub (audience "orcacloud"),
# so there is no secret to store, copy or rotate. The repository must be
# connected to exactly one of your Orca Cloud plugins under
# Edit plugin > GitHub publishing, otherwise the API answers 401.
#
#   PLUGIN_FILES="orcad_any.py" bash packaging/publish_orcacloud.sh
#
# Files must end in a supported target OS/arch suffix before .py/.whl, e.g.
# orcad_any.py, orcad_win_x86_64.py, orcad_linux_x86_64.py.
set -euo pipefail

: "${PLUGIN_FILES:?set PLUGIN_FILES to the files to upload}"
: "${ACTIONS_ID_TOKEN_REQUEST_URL:?this script only runs inside GitHub Actions}"
: "${ACTIONS_ID_TOKEN_REQUEST_TOKEN:?this script only runs inside GitHub Actions}"

files=()
for file in $PLUGIN_FILES; do
  [ -f "$file" ] && files+=("$file")
done
if [ ${#files[@]} -eq 0 ]; then
  echo "No plugin files matched: $PLUGIN_FILES" >&2
  exit 1
fi

# Build the metadata JSON and refuse a tag that disagrees with the plugin's own
# version, which the API would reject anyway with a less obvious message.
version="${GITHUB_REF_NAME#v}"
# A release event carries the notes in the event payload; a tag push does not,
# so fall back to the CHANGELOG.md section for this version.
changelog="$(python3 packaging/release_notes.py "v$version" 2>/dev/null || true)"
metadata="$(VERSION="$version" FILES="$PLUGIN_FILES" CHANGELOG="$changelog" python3 - <<'PY'
import json
import os
import pathlib
import re
import sys

version = os.environ["VERSION"]
event = json.loads(pathlib.Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
changelog = (event.get("release", {}).get("body") or "").strip() or os.environ.get("CHANGELOG", "").strip()
changelog = changelog[:4000]

for name in os.environ["FILES"].split():
    text = pathlib.Path(name).read_text(encoding="utf-8", errors="replace")
    declared = re.search(r'^# version = "([^"]+)"', text, re.M)
    if declared and declared.group(1) != version:
        sys.exit(f"{name} declares version {declared.group(1)}, but the tag is {version}")

metadata = {"version": version}
if changelog:
    metadata["changelog"] = changelog
print(json.dumps(metadata))
PY
)"

oidc_response="$(curl -sS -H "Authorization: Bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=orcacloud")" || {
  echo "::error title=Orca Cloud::could not request an OIDC token (is id-token: write granted?)"
  exit 1
}
oidc_token="$(printf '%s' "$oidc_response" | python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])' 2>/dev/null)" || {
  echo "::error title=Orca Cloud::the OIDC token request returned no token: ${oidc_response:0:400}"
  exit 1
}

args=(-F "metadata=$metadata")
for file in "${files[@]}"; do
  args+=(-F "files=@$file")
done
response="$(mktemp)"
trap 'rm -f "$response"' EXIT

status="$(curl -sS -o "$response" -w '%{http_code}' \
  -H "Authorization: Bearer $oidc_token" "${args[@]}" \
  https://api.orcaslicer.com/api/v1/plugin-publish/releases)"
body="$(cat "$response")"
echo "$body"
if [ "$status" != "201" ]; then
  case "$status" in
    401) reason="Orca Cloud rejected the identity from ${GITHUB_WORKFLOW_REF:-${GITHUB_WORKFLOW:-unknown}}: is $GITHUB_REPOSITORY connected to one of your plugins, and is that connection bound to this workflow?" ;;
    *)   reason="HTTP $status (a version error means the tag is not higher than the published one)" ;;
  esac
  echo "::error title=Orca Cloud publish failed::$reason: ${body:0:500}"
  echo "$reason" >&2
  exit 1
fi
echo "::notice title=Orca Cloud::published orcad $version"