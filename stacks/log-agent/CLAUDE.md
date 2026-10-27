# log-agent — Grafana Alloy on the machines other than the HP

Ships this machine's Docker container logs and systemd journal to the HP's Loki
(`stacks/logging`), through its push gateway `http://10.0.0.64:3100` (basic auth, user `agent`,
password `LOGGING__LOKI_PUSH_PASSWORD`). Host network; Alloy's debug UI is on `127.0.0.1:12345`
only. Read the logs in Grafana (`stacks/logging/CLAUDE.md`).

| machine | how it is deployed | extras |
|---|---|---|
| Jetson `10.0.0.179` | pipeline (`deploy/targets`), `/srv/homelab/log-agent`, env `prod` | `compose.prod.yaml`: Caddy access logs (`alloy/caddy.alloy`, `source="caddy"`) |
| pc5090 `10.0.0.71` | **by hand** (no runner), `~/log-agent` | none |

`alloy/common.alloy` is an identical copy of `stacks/logging/alloy/common.alloy` — edit both.

## pc5090 by hand
From a checkout on the Jetson (the password goes through a pipe, never onto the screen):
```bash
rsync -a --delete --exclude=/.env stacks/log-agent/ pc5090:log-agent/
ssh hp 'docker exec -i gh-runner python3 - get LOGGING__LOKI_PUSH_PASSWORD' < deploy/envvault.py \
  | ssh pc5090 'umask 077; { printf "LOKI_PASSWORD="; cat; echo; } > log-agent/.env'
ssh pc5090 'cd log-agent && docker compose --env-file .env --env-file env/pc5090.env up -d'
```
(`envvault.py get` runs inside the HP runner, which has `bws` and the prod token, and prints just
the value into the pipe. Any other way of getting the value out of Bitwarden works too.)
After a Renovate bump of the Alloy image, repeat the rsync + `up -d` on pc5090.
