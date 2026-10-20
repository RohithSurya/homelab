#!/usr/bin/env bash
# plan.sh <event> <before sha> <after sha> [stack|all] [dry_run]
# Prints matrix=<json> for GitHub Actions: which stacks to deploy and on which runner.
#  push:              every enabled stack; deploy.sh --if-changed then skips the ones whose files
#                     are unchanged since their last good deploy. (Diffing only this push would
#                     miss changes from a merge whose run GitHub cancelled: a concurrency group
#                     keeps one waiting run and cancels older waiting ones.)
#  workflow_dispatch: the requested stack (or all); not-yet-enabled stacks only with dry_run
set -euo pipefail
cd "$(dirname "$0")/.."
event=$1; want=${4:-all}; dry=${5:-false}   # $2 $3 (before/after sha) are no longer used
stacks=()
if [ "$event" = push ]; then
  mapfile -t stacks < <(ls stacks)
else
  if [ "$want" = all ]; then mapfile -t stacks < <(ls stacks); else stacks=("$want"); fi
fi
out=()
for s in "${stacks[@]}"; do
  line=$(awk -v s="$s" '$1==s && $0 !~ /^#/' deploy/targets)
  [ -n "$line" ] || continue
  read -r _ host _ enabled <<<"$line"
  if [ "$enabled" = yes ] || { [ "$event" != push ] && [ "$dry" = true ]; }; then
    out+=("{\"stack\":\"$s\",\"host\":\"$host\"}")
  fi
done
echo "matrix=[$(IFS=,; echo "${out[*]}")]"
