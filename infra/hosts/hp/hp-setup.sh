#!/usr/bin/env bash
# HP Pro Mini 400 G9 -> homelab prod host. Run once: sudo bash ~/hp-setup.sh
# Safe to re-run: every step checks before it acts. The disk wipe only happens if the
# USB disk is still un-partitioned for us (no partition labelled media/photos/backups).
set -euo pipefail

DISK_SERIAL="WD-WX52D812X87F"     # WD My Passport 5 TB (WD50NDZW)
LAN="10.0.0.0/24"
USER_NAME="rosurya"

say() { printf '\n== %s\n' "$*"; }
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }

# ---------------------------------------------------------------- packages
say "packages"
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -y -q curl ca-certificates      # needed to add the Tailscale repo
if [ ! -f /usr/share/keyrings/tailscale-archive-keyring.gpg ]; then
  . /etc/os-release
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.noarmor.gpg" \
    -o /usr/share/keyrings/tailscale-archive-keyring.gpg
  curl -fsSL "https://pkgs.tailscale.com/stable/ubuntu/${VERSION_CODENAME}.tailscale-keyring.list" \
    -o /etc/apt/sources.list.d/tailscale.list
fi
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 vainfo intel-media-va-driver-non-free \
  intel-gpu-tools nfs-common ufw parted tailscale
systemctl enable --now docker tailscaled

# ---------------------------------------------------------------- users / groups
say "users"
usermod -aG docker,render,video "$USER_NAME"
id ghrunner >/dev/null 2>&1 || useradd -m -s /bin/bash ghrunner
usermod -aG docker ghrunner

# ---------------------------------------------------------------- USB disk
say "disk"
DEV=""
for d in /sys/block/sd*; do
  n=$(basename "$d")
  [ "$(lsblk -dno SERIAL "/dev/$n" 2>/dev/null)" = "$DISK_SERIAL" ] && DEV="/dev/$n"
done
[ -n "$DEV" ] || { echo "disk with serial $DISK_SERIAL not found -- stopping"; exit 1; }
echo "disk = $DEV ($(lsblk -dno MODEL,SIZE "$DEV"))"

if lsblk -no LABEL "$DEV" | grep -qx media; then
  echo "already partitioned (label 'media' exists) -- not touching the disk"
else
  echo "Current contents of $DEV:"; lsblk -o NAME,SIZE,FSTYPE,LABEL "$DEV"
  read -r -p "WIPE $DEV and create media(2TB)/photos(1.5TB)/backups(rest)? type WIPE: " ok
  [ "$ok" = WIPE ] || { echo "aborted"; exit 1; }
  umount "${DEV}"* 2>/dev/null || true
  wipefs -a "$DEV"
  parted -s "$DEV" mklabel gpt \
    mkpart media   ext4 1MiB   2000GB \
    mkpart photos  ext4 2000GB 3500GB \
    mkpart backups ext4 3500GB 100%
  udevadm settle
  mkfs.ext4 -F -q -m 0 -L media   "${DEV}1"
  mkfs.ext4 -F -q -m 0 -L photos  "${DEV}2"
  mkfs.ext4 -F -q -m 0 -L backups "${DEV}3"
  udevadm settle
fi

for p in media photos backups; do
  # the desktop may have auto-mounted a fresh partition under /media/<user>/ -- undo that
  cur=$(findmnt -no TARGET "/dev/disk/by-label/$p" 2>/dev/null | head -1 || true)
  [ -n "$cur" ] && [ "$cur" != "/mnt/$p" ] && umount "$cur"
  mp=/mnt/$p
  uuid=$(blkid -s UUID -o value "/dev/disk/by-label/$p")
  if ! mountpoint -q "$mp"; then
    mkdir -p "$mp"
    chattr +i "$mp"              # nothing can write into the folder while the disk is NOT mounted
  fi
  grep -q "UUID=$uuid" /etc/fstab || \
    echo "UUID=$uuid  $mp  ext4  defaults,noatime,nofail,x-systemd.device-timeout=30s  0 2" >> /etc/fstab
done
systemctl daemon-reload
mount -a
for p in media photos backups; do mountpoint -q /mnt/$p && chown 1000:1000 /mnt/$p; done

# Docker must not start without the data disks (it would write into empty folders)
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/10-require-disks.conf <<'EOF'
[Unit]
RequiresMountsFor=/mnt/media /mnt/photos
EOF
systemctl daemon-reload

# ---------------------------------------------------------------- app folders
say "folders"
mkdir -p /srv/homelab /srv/appdata
chown ghrunner:ghrunner /srv/homelab
chown 1000:1000 /srv/appdata

# ---------------------------------------------------------------- firewall
# Allow only the LAN, Tailscale and Docker's own bridges. The Flint already blocks all
# inbound traffic from the internet (IPv4 except 443 -> Jetson, all IPv6), so this is a
# second layer, mainly for IPv6.
say "firewall"
ufw default deny incoming
ufw default allow outgoing
ufw allow from "$LAN"
ufw allow from 172.16.0.0/12        # docker bridges -> host-network services (Jellyfin, Bazarr)
ufw allow from fd00::/8             # LAN IPv6 (ULA)
ufw allow from fe80::/10            # link-local IPv6 (mDNS/DLNA)
ufw allow in on tailscale0
ufw --force enable

# ---------------------------------------------------------------- summary
say "done"
docker --version; docker compose version
lsblk -o NAME,SIZE,FSTYPE,LABEL,MOUNTPOINT "$DEV"
df -h /mnt/media /mnt/photos /mnt/backups
ufw status verbose | head -20
echo
echo "Next: run 'sudo tailscale up' on the HP and log in in the browser."
echo "Log out and back in (or reconnect SSH) so the docker group applies to $USER_NAME."
