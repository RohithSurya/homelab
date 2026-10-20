#!/usr/bin/env bash
# Jetson host firewall (ufw), with an automatic undo. Run: sudo bash jetson-ufw.sh
#
# Policy: everything from the LAN (IPv4 10.0.0.0/24, IPv6 link-local + ULA), Tailscale and Docker's
# own networks is allowed -- Home Assistant, Matter, HomeKit, Zigbee/MQTT, Samba, NFS for the HP and
# discovery (mDNS/SSDP) keep working; from anywhere else only Caddy's 443 (tcp + udp/HTTP3). The
# Flint router already blocks inbound IPv4 (except 443) and IPv6; this is the second layer.
#
# Safety: a 5-minute timer disables ufw again unless the checks below (run FROM the HP) all pass.
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo"; exit 1; }
USER_NAME=rosurya; ME=10.0.0.179; TS_ME=100.122.87.20   # probes run from the HP (ssh alias "hp")

command -v ufw >/dev/null || { apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q ufw; }
ufw --force reset >/dev/null           # start from a clean rule set (ufw is off at this point)
ufw default deny incoming
ufw default allow outgoing
ufw default allow routed               # leave Docker's forwarding alone
ufw allow from 10.0.0.0/24 comment 'LAN'
ufw allow from fe80::/10 comment 'IPv6 link-local (Matter, mDNS, HomeKit)'
ufw allow from fd00::/8 comment 'IPv6 ULA (LAN)'
ufw allow from 172.16.0.0/12 comment 'Docker networks -> host'
ufw allow in on tailscale0 comment 'Tailscale'
ufw allow 41641/udp comment 'Tailscale direct connections'
ufw allow 443/tcp comment 'Caddy HTTPS (router forward)'
ufw allow 443/udp comment 'Caddy HTTP/3'
ufw logging low

# the undo: armed BEFORE enabling
systemctl stop ufw-safety.timer 2>/dev/null || true
systemd-run --quiet --unit=ufw-safety --on-active=300 /usr/sbin/ufw --force disable
ufw --force enable
echo "ufw enabled -- automatic undo in 5 minutes unless all checks pass"
sleep 3

fails=0
check() { if sudo -u "$USER_NAME" ssh -o BatchMode=yes -o ConnectTimeout=8 hp "$2" >/dev/null 2>&1; then echo "  ok   $1"; else echo "  FAIL $1"; fails=$((fails+1)); fi; }
port() { check "$1 (tcp $2 from the HP)" "timeout 5 bash -c '</dev/tcp/$ME/$2'"; }
port ssh 22; port https 443; port "home assistant" 8123; port homekit 21064; port matter 5580
port mqtt 1883; port "jellyfin LAN pass-through" 8096
check "nfs: the HP reads the old movies" "ls /mnt/media/data/media/movies | head -1"
check "ssh over tailscale" "timeout 5 bash -c '</dev/tcp/$TS_ME/22'"

if [ "$fails" = 0 ]; then
  systemctl stop ufw-safety.timer
  echo "all checks passed -- ufw stays on (undo cancelled)"
  ufw status verbose | head -30
else
  ufw --force disable
  systemctl stop ufw-safety.timer 2>/dev/null || true
  echo "$fails check(s) failed -- ufw DISABLED again, nothing changed"
  exit 1
fi
