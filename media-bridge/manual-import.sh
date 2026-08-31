#!/usr/bin/env bash
# Manual-import a multi-movie pack that Radarr's auto-import can't handle.
#
# Seedr drops finished downloads into data/downloads/radarr/. A single folder
# holding several *different* movies (e.g. "Avengers Quadrilogy ...") never
# auto-imports, because Radarr maps one folder -> one movie. This walks Radarr's
# Manual Import API: scan the folder, let Radarr guess each file's movie, then
# move each .mkv into its library folder.
#
# Usage:
#   media-bridge/manual-import.sh "<folder>"                    # dry-run: show matches
#   media-bridge/manual-import.sh "<folder>" --import           # import (move)
#   media-bridge/manual-import.sh "<folder>" --import --movie N # force-map every file
#                                                                 to Radarr movie id N
#
# <folder> may be the bare folder name (assumed under data/downloads/radarr/) or any
# path that contains data/downloads/radarr/<name>. Without --movie, each file relies
# on Radarr's own guess; files it can't match show SKIP / Unknown Movie (usually a
# spelling difference between the release name and the TMDB title) — re-run with
# --movie <id> to override. The target movie(s) must already exist in your library.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RADARR_URL="${RADARR_URL:-http://127.0.0.1:7878}"

# shellcheck source=/dev/null
[ -f "$REPO/.env" ] && set -a && . "$REPO/.env" && set +a
: "${RADARR_API_KEY:?RADARR_API_KEY not set (expected in $REPO/.env)}"

ARG=""; DO_IMPORT=0; MOVIE_ID=""
while [ $# -gt 0 ]; do
  case "$1" in
    --import) DO_IMPORT=1 ;;
    --movie)  MOVIE_ID="${2:?--movie needs a Radarr movie id}"; shift ;;
    -*) echo "unknown flag: $1" >&2; exit 2 ;;
    *)  ARG="$1" ;;
  esac
  shift
done
[ -n "$ARG" ] || { echo "usage: $0 \"<folder>\" [--import] [--movie <id>]" >&2; exit 2; }

# Normalize to the container-visible path: /data/downloads/radarr/<name>
NAME="${ARG##*/data/downloads/radarr/}"   # strip everything up to the watch dir, if present
NAME="${NAME%/}"                          # drop trailing slash
NAME="${NAME##*/}"                         # if still a path, keep last component
CONTAINER_FOLDER="/data/downloads/radarr/$NAME"

[ -d "$REPO/data/downloads/radarr/$NAME" ] || {
  echo "not found on disk: $REPO/data/downloads/radarr/$NAME" >&2
  echo "available:" >&2; ls -1 "$REPO/data/downloads/radarr/" >&2 || true
  exit 1
}

RADARR_URL="$RADARR_URL" RADARR_API_KEY="$RADARR_API_KEY" \
CONTAINER_FOLDER="$CONTAINER_FOLDER" DO_IMPORT="$DO_IMPORT" MOVIE_ID="$MOVIE_ID" \
python3 - <<'PY'
import os, json, urllib.parse, urllib.request

base = os.environ["RADARR_URL"].rstrip("/")
key  = os.environ["RADARR_API_KEY"]
folder = os.environ["CONTAINER_FOLDER"]
do_import = os.environ["DO_IMPORT"] == "1"
override = os.environ.get("MOVIE_ID") or ""
override_id = int(override) if override else None

def call(method, path, body=None):
    url = base + path
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method=method,
        headers={"X-Api-Key": key, "Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))

q = urllib.parse.urlencode({"folder": folder, "filterExistingFiles": "true"})
scan = call("GET", "/api/v3/manualimport?" + q)
if not scan:
    print("nothing to import (folder empty or all files already imported)"); raise SystemExit(0)

ov_title = None
if override_id is not None:
    mv = call("GET", f"/api/v3/movie/{override_id}")  # fail loud if id is bogus
    ov_title = f"{mv['title']} {mv['year']}"
    print(f"--movie {override_id}: forcing every file to '{ov_title}'\n")

files, matched = [], 0
print(f"scanned {len(scan)} file(s) in:\n  {folder}\n")
for f in scan:
    m = f.get("movie") or {}
    rej = [r.get("reason") for r in f.get("rejections", [])]
    mid = override_id if override_id is not None else (m.get("id"))
    ok = mid is not None and (override_id is not None or not rej)
    matched += ok
    label = ov_title if override_id is not None else f"{m.get('title','<no match>')} {m.get('year','')}"
    print(f"  [{'OK ' if ok else 'SKIP'}] {f.get('relativePath')}")
    print(f"         -> {label}"
          f" | {(f.get('quality') or {}).get('quality',{}).get('name','?')}"
          + (f" | rejected: {rej}" if rej and override_id is None else ""))
    if ok:
        files.append({
            "path": f["path"], "folderName": f.get("folderName", ""),
            "movieId": mid, "quality": f["quality"],
            "languages": f.get("languages") or [{"id": 1, "name": "English"}],
            "releaseGroup": f.get("releaseGroup", ""),
            "indexerFlags": f.get("indexerFlags", 0),
        })

print(f"\n{matched}/{len(scan)} ready to import.")
if not do_import:
    print("dry-run — re-run with --import to move these into the library."); raise SystemExit(0)
if not files:
    print("nothing importable; aborting."); raise SystemExit(1)

res = call("POST", "/api/v3/command",
           {"name": "ManualImport", "importMode": "move", "files": files})
print(f"\nsubmitted ManualImport command {res['id']} ({res['status']}). "
      "Check Radarr -> Activity for progress.")
PY