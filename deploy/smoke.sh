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
  dashboard)
    check "homepage answers" has http://127.0.0.1:3010/api/healthcheck up ;;
  logging)
    check "loki ready (via gateway)" has http://127.0.0.1:3100/ready ready
    check "grafana /api/health"      has http://127.0.0.1:3020/api/health '"database": *"ok"'
    push_locked() { [ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 -X POST http://127.0.0.1:3100/loki/api/v1/push)" = 401 ]; }
    check "gateway refuses a push without the password" push_locked
    query_locked() { [ "$(curl -s -o /dev/null -w '%{http_code}' -m 10 http://127.0.0.1:3100/loki/api/v1/labels)" = 403 ]; }
    check "gateway refuses queries" query_locked
    hp_logs() {  # the HP's own agent has written something in the last 10 minutes
      local q
      q=$(python3 -c 'import urllib.parse; print(urllib.parse.quote("sum(count_over_time({host=\"hp\"}[10m]))"))')
      docker exec grafana wget -qO- "http://loki:3100/loki/api/v1/query?query=$q" | grep -q '"result":\[{'
    }
    check "loki holds recent HP logs" hp_logs ;;
  log-agent)
    agent_ready() { curl -fsS -m 10 http://127.0.0.1:12345/-/ready | grep -qi ready; }
    check "alloy ready" agent_ready
    no_push_errors() {  # loki.write reports failed pushes on its metrics; none since start
      ! curl -fsS -m 10 http://127.0.0.1:12345/metrics \
        | grep -E '^loki_write_dropped_entries_total\{.*reason="(http|ingester_error|rate_limited)' | grep -vq ' 0$'
    }
    check "alloy pushes reach the HP" no_push_errors ;;
  exposer)
    check "caddy config valid" docker exec caddy caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile
    check "fail2ban answers"   docker exec fail2ban fail2ban-client ping
    # geo filter: the live country database answers, and the loaded config carries the allow list
    check "geoip database answers" docker exec caddy geoip verify
    geo_loaded() { docker exec caddy sh -c 'caddy adapt --config /etc/caddy/Caddyfile 2>/dev/null' \
      | python3 -c 'import json,sys; c=json.dumps(json.load(sys.stdin)); sys.exit(0 if "\"allow_countries\": [\"" in c else 1)'; }
    check "geo filter has an allow list" geo_loaded
    for h in jellyfin immich nextcloud; do
      site_up() { local c; c=$(curl -sk -o /dev/null -w '%{http_code}' -m 10 --resolve "$1.rosurya.duckdns.org:443:127.0.0.1" "https://$1.rosurya.duckdns.org/"); [ "${c:0:1}" != 5 ] && [ "$c" != 000 ]; }
      check "https://$h.rosurya.duckdns.org answers" site_up "$h"
    done ;;
  *) die "no smoke checks for $stack" ;;
esac
[ "$fails" = 0 ] || die "$fails check(s) failed for $stack"
log "all checks passed for $stack"
