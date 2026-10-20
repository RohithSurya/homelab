#!/usr/bin/env bash
# plan.sh <event> <before sha> <after sha> [stack|all] [dry_run]
# Prints matrix=<json> for GitHub Actions: which stacks to deploy and on which runner.
#  push:              stacks with changed files under stacks/<stack>/ that are enabled in targets
#  workflow_dispatch: the requested stack (or all); not-yet-enabled stacks only with dry_run
set -euo pipefail
cd "$(dirname "$0")/.."
event=$1; before=$2; after=$3; want=${4:-all}; dry=${5:-false}
stacks=()
if [ "$event" = push ]; then
  if [ -z "$before" ] || [ "$before" = 0000000000000000000000000000000000000000 ] || ! git cat-file -e "$before" 2>/dev/null; then
    mapfile -t stacks < <(ls stacks)
  else
    mapfile -t stacks < <(git diff --name-only "$before" "$after" -- stacks | cut -d/ -f2 | sort -u)
  fi
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
