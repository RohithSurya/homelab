#!/usr/bin/env bash
# smoke.sh <stack>: is the stack actually serving? Retries for up to ~3 minutes (apps that just
# started need a moment). The runner uses the host network, so 127.0.0.1 is the host.
set -euo pipefail
. "$(dirname "$0")/lib.sh"
stack=${1:?usage: smoke.sh <stack>}
fails=0
check() {  # check <name> <command...>
  local name=$1; shift
  for _ in $(seq 1 18); do
    if out=$("$@" 2>&1); then log "ok   $name"; return 0; fi
    sleep 10
  done
  log "FAIL $name: $(echo "$out" | tail -1 | cut -c1-200)"; fails=$((fails + 1))
}
get() { curl -fsS -m 10 "$1"; }
has() { get "$1" | grep -q -- "$2"; }

case $stack in
  media-server)
    check "jellyfin /health"      has http://127.0.0.1:8096/health Healthy
    check "radarr ping"           has http://127.0.0.1:7878/ping OK
    check "sonarr ping"           has http://127.0.0.1:8989/ping OK
    check "prowlarr ping"         has http://127.0.0.1:9696/ping OK
    check "movierulz torznab caps" has "http://127.0.0.1:8002/torznab/api?t=caps" '<caps'
    check "tamilmv torznab caps"  has "http://127.0.0.1:8005/torznab/api?t=caps" '<caps'
    check "bridge status page"    has http://127.0.0.1:8003/bridge/status.json '{'
    vpn_differs() {
      local home vpn
      home=$(curl -fsS -m 10 https://ipinfo.io/ip)
      vpn=$(docker exec gluetun wget -qO- -T 10 https://ipinfo.io/ip)
      [ -n "$vpn" ] && [ "$vpn" != "$home" ]
    }
    check "gluetun exit IP is not the home IP" vpn_differs ;;
  immich)
    check "immich /api/server/ping" has http://127.0.0.1:2283/api/server/ping pong ;;
  nextcloud)
    nc_ok() { local s; s=$(get http://127.0.0.1:8081/status.php); echo "$s" | grep -q '"installed":true' && echo "$s" | grep -q '"maintenance":false'; }
    check "nextcloud status.php" nc_ok ;;
  exposer)
    check "caddy config valid" docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
    check "fail2ban answers"   docker exec fail2ban fail2ban-client ping
    for h in jellyfin immich nextcloud; do
      site_up() { local c; c=$(curl -sk -o /dev/null -w '%{http_code}' -m 10 --resolve "$1.rosurya.duckdns.org:443:127.0.0.1" "https://$1.rosurya.duckdns.org/"); [ "${c:0:1}" != 5 ] && [ "$c" != 000 ]; }
      check "https://$h.rosurya.duckdns.org answers" site_up "$h"
    done ;;
  *) die "no smoke checks for $stack" ;;
esac
[ "$fails" = 0 ] || die "$fails check(s) failed for $stack"
log "all checks passed for $stack"
