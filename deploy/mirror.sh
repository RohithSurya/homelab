#!/usr/bin/env bash
# mirror.sh: keep a second copy of what can't be re-downloaded on ANOTHER machine.
# The HP's photos (Immich), files (Nextcloud) and backup folder live on one USB disk; this mirrors
# them to the Jetson's NVMe ($MIRROR_TARGET) with rsync over SSH. Deletions are mirrored too, but
# only after the source is checked to be mounted and non-empty -- an unmounted disk must never
# wipe the copy. Runs in a throwaway root container (some files are owned by www-data/root);
# the SSH key and the pinned host key come from the host user's ~/.ssh.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
: "${MIRROR_TARGET:?no MIRROR_TARGET in deploy/hosts/$HOMELAB_HOST.env}"
: "${MIRROR_SOURCES:?no MIRROR_SOURCES in deploy/hosts/$HOMELAB_HOST.env}"
: "${MIRROR_SSH_DIR:?no MIRROR_SSH_DIR in deploy/hosts/$HOMELAB_HOST.env}"

for src in $MIRROR_SOURCES; do
  # guard: the USB partition holding $src (/mnt/<name>) must be mounted -- unmounted, only the
  # empty immutable mount-point folder is left -- and $src must contain files
  disk=$(echo "$src" | cut -d/ -f1-3)            # /mnt/photos/immich-library -> /mnt/photos
  mountpoint -q "$disk" && [ "$disk" != /mnt ] || die "$disk is not mounted -- not mirroring $src"
  n=$(docker run --rm -v "$src:/s:ro" alpine sh -c 'find /s -type f | head -1000 | wc -l')
  [ "$n" -gt 0 ] || die "$src is empty -- not mirroring (would wipe the copy)"
  name=$(basename "$src")
  log "mirror $src -> $MIRROR_TARGET/$name"
  docker run --rm -v "$src:/s:ro" -v "$MIRROR_SSH_DIR/id_ed25519:/k/id:ro" -v "$MIRROR_SSH_DIR/known_hosts:/k/known_hosts:ro" \
    alpine sh -c 'apk add -q rsync openssh-client >/dev/null && cp /k/id /tmp/id && chmod 600 /tmp/id &&
      rsync -a --delete --no-o --no-g --stats \
        -e "ssh -i /tmp/id -o UserKnownHostsFile=/k/known_hosts -o StrictHostKeyChecking=yes -o BatchMode=yes" \
        /s/ '"$MIRROR_TARGET/$name/" \
    | grep -E 'Number of (regular files transferred|deleted files)|Total file size' | sed 's/^/   /'
done
log "mirror done"
