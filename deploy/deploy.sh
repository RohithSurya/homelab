#!/usr/bin/env bash
# deploy.sh <stack> [--dry-run] [--src DIR] [--sha SHA] [--no-backup]
#
# Copies stacks/<stack> to $DEPLOY_BASE/<stack> (runtime state listed in the stack's .gitignore
# is never touched), renders .env from Bitwarden, backs up, builds and starts the stack, applies
# the gluetun rule, then runs smoke.sh. Exit != 0 means the caller should run rollback.sh.
# --dry-run: everything up to and including image builds, into $DEPLOY_BASE/.dryrun/<stack>,
#            nothing is started or stopped (works for stacks not enabled yet).
set -euo pipefail
. "$(dirname "$0")/lib.sh"

stack=${1:?usage: deploy.sh <stack> [--dry-run] [--src DIR] [--sha SHA] [--no-backup]}; shift
dry=0; backup=1; src="$REPO/stacks/$stack"; sha=$(git -C "$REPO" rev-parse HEAD)
while [ $# -gt 0 ]; do
  case $1 in
    --dry-run) dry=1 ;;
    --no-backup) backup=0 ;;
    --src) src=$2; shift ;;
    --sha) sha=$2; shift ;;
    *) die "unknown option $1" ;;
  esac
  shift
done

target "$stack"
[ "$T_HOST" = "$HOMELAB_HOST" ] || die "$stack deploys on $T_HOST, this is $HOMELAB_HOST"
[ "$T_ENABLED" = yes ] || [ "$dry" = 1 ] || die "$stack is not enabled in deploy/targets (cutover not done yet)"
[ -d "$src" ] || die "no source folder $src"

dir="$DEPLOY_BASE/$stack"
[ "$dry" = 1 ] && dir="$DEPLOY_BASE/.dryrun/$stack"
mkdir -p "$dir"
log "$stack @ ${sha:0:7} -> $dir ($T_ENV$([ "$dry" = 1 ] && echo ', DRY RUN'))"

# 1. files. The stack's .gitignore lists its runtime state; rsync neither copies nor deletes it.
rsync -a --delete --filter=':- .gitignore' \
  --exclude=/.env --exclude=/.deployed --exclude=/.previous "$src/" "$dir/"

# 2. secrets
python3 "$DEPLOY_DIR_SELF/envvault.py" render "$dir/env.tpl" "$dir/.env.new"
mv "$dir/.env.new" "$dir/.env"; chmod 600 "$dir/.env"

read -r -a args <<<"$(compose_args "$dir" "$T_ENV")"
dc() { docker compose "${args[@]}" "$@"; }
dc config -q
log "compose config valid"

# 3. images (local builds are tagged with the commit as well, so a rollback can find them)
if dc config --format json | python3 -c 'import json,sys; sys.exit(0 if any("build" in s for s in json.load(sys.stdin)["services"].values()) else 1)'; then
  dc build --pull
fi
if [ "$dry" = 1 ]; then
  dc pull --quiet --ignore-buildable 2>/dev/null || dc pull --quiet --ignore-pull-failures || true
  log "dry run OK: config valid, images built/pulled, nothing started"
  exit 0
fi

# 4. backup what is running now
if [ "$backup" = 1 ] && [ -n "$(dc ps -q 2>/dev/null)" ]; then
  "$DEPLOY_DIR_SELF/backup.sh" "$stack" "$dir" "$sha"
fi

# 5. start. gluetun rule (CLAUDE.md): if gluetun was recreated, its netns dependents must be too.
gl_before=$(docker inspect -f '{{.Id}}' gluetun 2>/dev/null || true)
dc up -d --remove-orphans --wait --wait-timeout 600
if dc config --services | grep -qx gluetun; then
  gl_after=$(docker inspect -f '{{.Id}}' gluetun 2>/dev/null || true)
  if [ -n "$gl_before" ] && [ "$gl_before" != "$gl_after" ]; then
    log "gluetun was recreated -> recreating prowlarr flaresolverr sonarr radarr"
    dc up -d --force-recreate --no-deps --wait --wait-timeout 300 prowlarr flaresolverr sonarr radarr
  fi
fi

# 6. checks
"$DEPLOY_DIR_SELF/smoke.sh" "$stack"

[ -f "$dir/.deployed" ] && cp "$dir/.deployed" "$dir/.previous"
echo "$sha" > "$dir/.deployed"
log "$stack deployed @ ${sha:0:7}"
