# Host setup scripts

One-off, root-run scripts that prepared the machines (kept for the record and for rebuilding a host).

| Script | Host | What it does |
|---|---|---|
| `hp/hp-setup.sh` | HP | Docker + Compose, Intel GPU drivers, NFS client, ufw, Tailscale; USB disk → `media`/`photos`/`backups` (checks the disk serial, asks for `WIPE`); Docker waits for the disks |
| `hp/hp-nfs.sh` | HP | Automounts the Jetson's old movies (NFSv3, `soft`) at `/mnt/media/data/media/movies{,-private}` |
| `hp/homeops-docker` | HP | `~/bin/homeops-docker`: SSH forced command for Hermes' homeops key (4 Docker API calls, restart allowlist) |
| `jetson/jetson-nfs.sh` | Jetson | NFS-exports only the two old-movie folders, only to the HP |
| `jetson/jetson-ufw.sh` | Jetson | Host firewall: LAN/Tailscale/Docker + 443; 5-minute automatic undo unless checks from the HP pass |
