# Immich stack configuration. Standalone (mirrors ../next-cloud/.env).

# Where uploaded photos/videos live (Immich-managed storage). Resolved relative to this dir.
UPLOAD_LOCATION={{vault:IMMICH__UPLOAD_LOCATION}}

# Postgres data (local disk only — network shares are unsupported by Immich).
DB_DATA_LOCATION={{vault:IMMICH__DB_DATA_LOCATION}}

# Timezone (matches the rest of the media-server stack).
TZ={{vault:IMMICH__TZ}}

# IMMICH_VERSION lives in env/prod.env (not a secret; Renovate updates it there by PR).

# Database credentials (alphanumeric password — Immich rejects special characters here).
DB_PASSWORD={{vault:IMMICH__DB_PASSWORD}}
DB_USERNAME={{vault:IMMICH__DB_USERNAME}}
DB_DATABASE_NAME={{vault:IMMICH__DB_DATABASE_NAME}}

# Machine learning settings
MACHINE_LEARNING_MODEL_REVISION={{vault:IMMICH__MACHINE_LEARNING_MODEL_REVISION}}
