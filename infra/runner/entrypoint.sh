#!/usr/bin/env bash
# Registers the runner once (state kept in the /state volume), then runs it.
# First start needs RUNNER_TOKEN (a registration token, valid 1 h); later starts reuse /state.
set -euo pipefail
: "${RUNNER_URL:?}" "${RUNNER_NAME:?}" "${RUNNER_LABELS:?}"
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
  sudo -u "$USER_NAME" -E /opt/runner/config.sh --unattended --replace \
    --url "$RUNNER_URL" --token "$RUNNER_TOKEN" --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" --work /opt/runner/_work
  for f in .runner .credentials .credentials_rsaparams; do
    mv "/opt/runner/$f" "/state/$f" && ln -sf "/state/$f" "/opt/runner/$f"
  done
fi
exec sudo -u "$USER_NAME" -E --preserve-env=HOMELAB_HOST,BWS_TOKEN_FILE /opt/runner/run.sh
