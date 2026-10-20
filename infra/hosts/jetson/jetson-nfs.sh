#!/usr/bin/env bash
# Jetson: share ONLY the old movie folders with the HP (10.0.0.64), read-write.
# Run once: sudo bash ~/jetson-nfs.sh   (safe to re-run)
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

HP=10.0.0.64
BASE=/home/rosurya/media-server/data/media
OPTS="rw,sync,no_subtree_check,root_squash,sec=sys"

apt-get install -y -q nfs-kernel-server

for d in movies movies-private; do
  line="$BASE/$d  $HP($OPTS)"
  grep -qF "$BASE/$d " /etc/exports || echo "$line" >> /etc/exports
done
systemctl enable --now nfs-server
exportfs -ra
echo; exportfs -v
