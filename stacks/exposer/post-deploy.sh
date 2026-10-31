#!/usr/bin/env bash
# Run by deploy/deploy.sh after `up`: Caddy, fail2ban and CrowdSec read their config only at start
# or reload, and their config files are bind-mounted, so a deploy that only changes those files
# recreates nothing. Reload all three so every change takes effect. Caddy's and fail2ban's reloads
# are no-ops when nothing changed; CrowdSec's (SIGHUP) briefly pauses the WAF (~5 s, requests pass
# unchecked meanwhile -- fail-open).
set -euo pipefail
docker exec caddy caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | grep -iv 'not formatted' || true
docker exec fail2ban fail2ban-client reload >/dev/null
docker exec crowdsec sh -c 'kill -HUP "$(pidof crowdsec)"'
echo "caddy + fail2ban + crowdsec reloaded"
