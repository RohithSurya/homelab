# Bitwarden homelab-prod. GRAFANA_ADMIN_PASSWORD = Grafana's "admin" login.
# LOKI_PUSH_PASSWORD = what the other machines' Alloy agents send to the push gateway (user "agent").
# The log-agent stack (Jetson, pc5090) reads the same value: homelab-dev holds a copy for the Jetson.
GRAFANA_ADMIN_PASSWORD={{vault:LOGGING__GRAFANA_ADMIN_PASSWORD}}
LOKI_PUSH_PASSWORD={{vault:LOGGING__LOKI_PUSH_PASSWORD}}
# The ops ntfy topic (shared with media-bridge and the pipeline): Grafana's log alerts go there.
NTFY_TOPIC={{vault:MEDIA__NTFY_TOPIC}}
