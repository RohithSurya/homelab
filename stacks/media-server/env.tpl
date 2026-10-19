# Proton VPN WireGuard private key.
# Get it from: account.protonvpn.com -> Downloads -> WireGuard configuration
# Copy the value of the "PrivateKey =" line from the generated .conf file.

WIREGUARD_PRIVATE_KEY={{vault:MEDIA__WIREGUARD_PRIVATE_KEY}}
# Seedr account (for the seedr-bridge container). Used with Seedr's REST API.
SEEDR_EMAIL={{vault:MEDIA__SEEDR_EMAIL}}
# password is base64-encoded so Docker/shell '$' interpolation can't corrupt it
SEEDR_PASSWORD_B64={{vault:MEDIA__SEEDR_PASSWORD_B64}}

# seedr-bridge stalled-download handling: *arr APIs (via gluetun) + ntfy notifications
RADARR_API_KEY={{vault:MEDIA__RADARR_API_KEY}}
SONARR_API_KEY={{vault:MEDIA__SONARR_API_KEY}}
NTFY_TOPIC={{vault:MEDIA__NTFY_TOPIC}}
# DIUN image-update alerts go to their OWN topic: the shared one carries recurring
# operational alerts that bury a once-daily batch in the app's per-topic group.
NTFY_TOPIC_DIUN={{vault:MEDIA__NTFY_TOPIC_DIUN}}

# jellyfin-reaper: auto-delete movies watched by ALL users (after a grace window).
# Mint this in the Jellyfin UI: Dashboard -> API Keys -> '+' (name it "reaper").
JELLYFIN_API_KEY={{vault:MEDIA__JELLYFIN_API_KEY}}

# movierulz Torznab feed apikey (also entered in Prowlarr)
MOVIERULZ_APIKEY={{vault:MEDIA__MOVIERULZ_APIKEY}}
# Base URL of the movierulz site to crawl (no trailing slash)
MOVIERULZ_BASE={{vault:MEDIA__MOVIERULZ_BASE}}

# DaddyLive (DLHD) site base URL for the Jellyfin live-TV resolver (no trailing slash).
DLHD_BASE={{vault:MEDIA__DLHD_BASE}}


# DUCKDNS_TOKEN moved to ../exposer/.env with the duckdns service.

# tamilmv/tamilblasters Torznab feed apikey (also entered in Prowlarr)
TAMILMV_APIKEY={{vault:MEDIA__TAMILMV_APIKEY}}

# --- seedbox backend (Pulsed Media M1000 SSD 230G, service #54606/#73373, Helsinki) ---
SEEDBOX_QBIT_USER={{vault:MEDIA__SEEDBOX_QBIT_USER}}
SEEDBOX_SSH_HOST={{vault:MEDIA__SEEDBOX_SSH_HOST}}
SEEDBOX_SSH_USER={{vault:MEDIA__SEEDBOX_SSH_USER}}
