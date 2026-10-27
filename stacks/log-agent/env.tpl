# Push gateway password (user "agent"). The Jetson renders it from homelab-dev (a copy of the
# homelab-prod value the HP's logging stack checks against). pc5090 has no runner: its .env is
# written by hand (see CLAUDE.md).
LOKI_PASSWORD={{vault:LOGGING__LOKI_PUSH_PASSWORD}}
