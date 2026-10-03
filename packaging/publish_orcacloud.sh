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
metadata="$(FILES="$PLUGIN_FILES" python3 - <<'PY'
import json
import os
import pathlib
import re
import sys

version = os.environ["GITHUB_REF_NAME"].removeprefix("v")
event = json.loads(pathlib.Path(os.environ["GITHUB_EVENT_PATH"]).read_text(encoding="utf-8"))
changelog = (event.get("release", {}).get("body") or "").strip()[:4000]

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

oidc_token="$(curl -sS -H "Authorization: Bearer $ACTIONS_ID_TOKEN_REQUEST_TOKEN" \
  "${ACTIONS_ID_TOKEN_REQUEST_URL}&audience=orcacloud" | python3 -c 'import json,sys; print(json.load(sys.stdin)["value"])')"

args=(-F "metadata=$metadata")
for file in "${files[@]}"; do
  args+=(-F "files=@$file")
done
response="$(mktemp)"
trap 'rm -f "$response"' EXIT

status="$(curl -sS -o "$response" -w '%{http_code}' \
  -H "Authorization: Bearer $oidc_token" "${args[@]}" \
  https://api.orcaslicer.com/api/v1/plugin-publish/releases)"
cat "$response"
echo
if [ "$status" != "201" ]; then
  case "$status" in
    401) echo "Orca Cloud rejected the identity: is $GITHUB_REPOSITORY connected to one of your plugins?" >&2 ;;
    *)   echo "Orca Cloud publish failed with HTTP $status (a version error means the tag is not higher than the published one)" >&2 ;;
  esac
  exit 1
fi