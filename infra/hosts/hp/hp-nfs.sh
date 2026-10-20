#!/usr/bin/env bash
# HP: mount the Jetson's old movie folders (NFS v3) where the media stack expects them.
# Run once: sudo bash ~/hp-nfs.sh   (safe to re-run)
#
# automount + soft: if the Jetson is off, opening these folders FAILS with an error instead of
# showing an empty folder -- an empty folder would make Jellyfin/Radarr think the movies are gone.
# Nothing waits on the Jetson at boot either (it mounts on first access).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

JETSON=10.0.0.179
SRC=/home/rosurya/media-server/data/media
DST=/mnt/media/data/media
OPTS="nfsvers=3,soft,timeo=100,retrans=3,noatime,_netdev,nofail,x-systemd.automount,x-systemd.mount-timeout=20s,x-systemd.idle-timeout=0"

for d in movies movies-private; do
  mkdir -p "$DST/$d"
  grep -qF " $DST/$d " /etc/fstab || echo "$JETSON:$SRC/$d $DST/$d nfs $OPTS 0 0" >> /etc/fstab
done
systemctl daemon-reload
systemctl restart remote-fs.target
for d in movies movies-private; do
  printf '%-16s %s folders\n' "$d" "$(ls "$DST/$d" | wc -l)"
done
findmnt -t nfs,autofs -o TARGET,SOURCE,FSTYPE,OPTIONS | cut -c1-150
