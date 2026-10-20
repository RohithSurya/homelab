# shellcheck shell=bash disable=SC2034
# Shared helpers for the deploy scripts. Source it; don't run it.
# (SC2034: REPO and T_* are used by the scripts that source this file.)
DEPLOY_DIR_SELF=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
REPO=$(dirname "$DEPLOY_DIR_SELF")

log() { printf '[%s] %s\n' "$(date +%H:%M:%S)" "$*" >&2; }
die() { log "ERROR: $*"; exit 1; }

: "${HOMELAB_HOST:?set HOMELAB_HOST (hp or jetson); the runner container sets it}"
[ -f "$DEPLOY_DIR_SELF/hosts/$HOMELAB_HOST.env" ] || die "no deploy/hosts/$HOMELAB_HOST.env"
# shellcheck source=/dev/null
. "$DEPLOY_DIR_SELF/hosts/$HOMELAB_HOST.env"

# target <stack> -> sets T_HOST T_ENV T_ENABLED
target() {
  local line
  line=$(awk -v s="$1" '$1==s && $0 !~ /^#/' "$DEPLOY_DIR_SELF/targets")
  [ -n "$line" ] || die "stack '$1' is not in deploy/targets"
  read -r _ T_HOST T_ENV T_ENABLED <<<"$line"
}

# compose_args <dir> <env> -> prints the -f / --env-file arguments for that stack folder
compose_args() {
  local dir=$1 env=$2 base
  base=$(ls "$dir"/docker-compose.y*ml 2>/dev/null | head -1)
  [ -n "$base" ] || die "no docker-compose.y(a)ml in $dir"
  printf -- '--project-directory %s -f %s' "$dir" "$base"
  [ -f "$dir/compose.$env.yaml" ] && printf -- ' -f %s' "$dir/compose.$env.yaml"
  printf -- ' --env-file %s' "$dir/.env"
  [ -f "$dir/env/$env.env" ] && printf -- ' --env-file %s' "$dir/env/$env.env"
  return 0
}

notify() {  # notify <title> <message> [priority]
  local topic
  topic=$(python3 "$DEPLOY_DIR_SELF/envvault.py" get MEDIA__NTFY_TOPIC 2>/dev/null) || return 0
  curl -fsS -m 15 -H "Title: $1" -H "Priority: ${3:-default}" -H "Tags: rocket" \
    -d "$2" "https://ntfy.sh/$topic" >/dev/null || true
}
