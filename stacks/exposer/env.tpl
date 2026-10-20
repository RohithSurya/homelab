# Edge stack secrets — keep private (gitignored).

# DuckDNS account token: duckdns.org -> your token (top of the page).
# Keeps rosurya.duckdns.org (and every *.rosurya.duckdns.org vhost) pointed at
# the current dynamic Comcast WAN IP.
DUCKDNS_TOKEN={{vault:EXPOSER__DUCKDNS_TOKEN}}

# CrowdSec LAPI key shared by the crowdsec agent and its firewall bouncer.
CROWDSEC_BOUNCER_KEY={{vault:EXPOSER__CROWDSEC_BOUNCER_KEY}}

# DIUN image-update notices (own ntfy topic -- never the shared ops topic).
NTFY_TOPIC_DIUN={{vault:EXPOSER__NTFY_TOPIC_DIUN}}