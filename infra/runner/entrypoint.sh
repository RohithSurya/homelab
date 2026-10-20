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
  setpriv --reuid="$RUN_AS" --regid="$(id -g "$USER_NAME")" --init-groups \
    env HOME="$(getent passwd "$RUN_AS" | cut -d: -f6)" /opt/runner/config.sh --unattended --replace \
    --url "$RUNNER_URL" --token "$RUNNER_TOKEN" --name "$RUNNER_NAME" \
    --labels "$RUNNER_LABELS" --work "$RUNNER_WORK"
  for f in .runner .credentials .credentials_rsaparams; do
    mv "/opt/runner/$f" "/state/$f" && ln -sf "/state/$f" "/opt/runner/$f"
  done
fi
# setpriv (not sudo) so the runner IS this process: docker stop's SIGINT (STOPSIGNAL) reaches
# Runner.Listener, which then ends its GitHub session cleanly. Through sudo + run.sh the signal
# was lost, the old session lingered for minutes after every restart, and GitHub handed queued
# jobs to that dead session where they sat forever.
home=$(getent passwd "$RUN_AS" | cut -d: -f6)
cd /opt/runner
exec setpriv --reuid="$RUN_AS" --regid="$(id -g "$USER_NAME")" --init-groups \
  env HOME="$home" USER="$USER_NAME" HOMELAB_HOST="${HOMELAB_HOST:-}" BWS_TOKEN_FILE="${BWS_TOKEN_FILE:-}" \
      PATH="/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin" \
  /opt/runner/bin/Runner.Listener run
