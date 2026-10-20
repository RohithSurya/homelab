#!/usr/bin/env bash
# rollback.sh <stack>: redeploy the last version that passed its checks (the .deployed of the
# failed run's predecessor). Code, config and images only -- databases are NOT restored
# automatically (a restore would drop whatever was written since the backup); the
# pre-deploy dump is under $BACKUP_BASE/<stack>/ and the alert says so.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
stack=${1:?usage: rollback.sh <stack>}
target "$stack"
dir="$DEPLOY_BASE/$stack"
good=$(cat "$dir/.deployed" 2>/dev/null || true)
[ -n "$good" ] || die "no previous good version recorded for $stack -- roll back by hand"
tmp=$(mktemp -d); trap 'rm -rf "$tmp"' EXIT
git -C "$REPO" archive "$good" "stacks/$stack" "deploy" | tar -x -C "$tmp"
log "rolling $stack back to ${good:0:7}"
"$DEPLOY_DIR_SELF/deploy.sh" "$stack" --src "$tmp/stacks/$stack" --sha "$good" --no-backup
