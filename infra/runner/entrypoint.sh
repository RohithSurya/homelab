#!/usr/bin/env bash
# Registers the runner once (state kept in the /state volume), then runs it.
# First start needs RUNNER_TOKEN (a registration token, valid 1 h); later starts reuse /state.
set -euo pipefail
: "${RUNNER_URL:?}" "${RUNNER_NAME:?}" "${RUNNER_LABELS:?}" "${RUNNER_WORK:?}"
# RUNNER_WORK is bind-mounted at the SAME path as on the host: jobs start helper containers on the
# host's Docker with -v "$PWD:..." and the host can only mount paths it actually has.
RUN_AS="${RUN_AS_UID:-1000}"

# The runner refuses to run as root; run it as the host user that owns the deploy folders.
if ! getent passwd "$RUN_AS" >/dev/null; then
  useradd -u "$RUN_AS" -M -d /opt/runner -s /bin/bash runner
fi
USER_NAME=$(getent passwd "$RUN_AS" | cut -d: -f1)
if [ -n "${DOCKER_GID:-}" ]; then
  getent group "$DOCKER_GID" >/dev/null || groupadd -g "$DOCKER_GID" hostdocker
  usermod -aG "$(getent group "$DOCKER_GID" | cut -d: -f1)" "$USER_NAME"
fi

mkdir -p /state
for f in .runner .credentials .credentials_rsaparams; do
  [ -e "/state/$f" ] && ln -sf "/state/$f" "/opt/runner/$f"
done
chown -R "$RUN_AS" /opt/runner /state

if [ ! -e /state/.runner ]; then
  : "${RUNNER_TOKEN:?first start needs RUNNER_TOKEN}"
  sudo -u "$USER_NAME" -H /opt/runner/config.sh --unattended --replace \
    --url "$RUNNER_URL" --token "$RUNNER_TOKEN" --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" --work "$RUNNER_WORK"
  for f in .runner .credentials .credentials_rsaparams; do
    mv "/opt/runner/$f" "/state/$f" && ln -sf "/state/$f" "/opt/runner/$f"
  done
fi
# -H: the job's HOME is the runner user's own (not /root, which it can't read -> checkout fails)
# keep an older registration's work folder in step with RUNNER_WORK
tmp=$(mktemp); jq --arg w "$RUNNER_WORK" '.workFolder = $w' /state/.runner > "$tmp" && cat "$tmp" > /state/.runner && rm -f "$tmp"
mkdir -p "$RUNNER_WORK" && chown "$RUN_AS" "$RUNNER_WORK"
exec sudo -u "$USER_NAME" -H --preserve-env=HOMELAB_HOST,BWS_TOKEN_FILE /opt/runner/run.sh
