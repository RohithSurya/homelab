#!/usr/bin/env bash
# install.sh <hp|jetson> <registration token>
# Builds the runner image on this machine and starts it as a container (restart: unless-stopped).
# Get a token (valid 1 h) with:
#   gh api -X POST repos/RohithSurya/homelab/actions/runners/registration-token --jq .token
# Re-running replaces the container; the registration in the gh-runner-state volume is kept.
set -euo pipefail
host=${1:?usage: install.sh <hp|jetson> <token>}; token=${2:-}
cd "$(dirname "$0")"
case $host in
  hp)     labels=hp,prod;    mounts=(-v /srv/homelab:/srv/homelab -v /mnt:/mnt) ;;
  jetson) labels=jetson,dev; mounts=() ;;
  *) echo "host must be hp or jetson"; exit 1 ;;
esac
tok_file=$HOME/.config/homelab/bws_token        # this machine's Bitwarden machine-account token
[ -f "$tok_file" ] && mounts+=(-v "$tok_file:/run/secrets/bws_token:ro")

docker build -q -t homelab-runner:latest . >/dev/null
docker rm -f gh-runner >/dev/null 2>&1 || true
docker run -d --name gh-runner --restart unless-stopped --network host \
  -e RUNNER_URL=https://github.com/RohithSurya/homelab -e RUNNER_NAME="$host" -e RUNNER_LABELS="$labels" \
  -e RUNNER_TOKEN="$token" -e RUN_AS_UID="$(id -u)" -e DOCKER_GID="$(getent group docker | cut -d: -f3)" \
  -e HOMELAB_HOST="$host" -e BWS_TOKEN_FILE=/run/secrets/bws_token \
  -v /var/run/docker.sock:/var/run/docker.sock -v gh-runner-state:/state "${mounts[@]}" \
  --label autoheal=false homelab-runner:latest >/dev/null
sleep 15; docker logs --tail 5 gh-runner
