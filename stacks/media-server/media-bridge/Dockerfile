FROM python:3.12-slim

WORKDIR /app
# 'docker' SDK lets the youtube component spawn disposable yt-dlp+ffmpeg containers
# via the host Docker socket (yt-dlp/ffmpeg stay out of this image). 'paramiko' is the
# seedbox backend's SFTP client (PKey.from_path needs >= 3.2).
RUN pip install --no-cache-dir requests docker "paramiko>=3.2"
# unrar: scene releases (private trackers) come as RAR volumes that Radarr/Sonarr never unpack; the
# pull worker extracts them in staging before delivery. Debian ships it in non-free.
RUN sed -i 's/^Components: main$/Components: main non-free/' /etc/apt/sources.list.d/debian.sources \
 && apt-get update && apt-get install -y --no-install-recommends unrar \
 && rm -rf /var/lib/apt/lists/*
# Bitwarden Secrets Manager CLI (vault.py). Pinned + checksum-verified release asset, per CPU
# architecture: the Jetson (dev) is arm64, the HP (prod) is amd64. Checksums are the ones Bitwarden
# publishes in bws-sha256-checksums-<version>.txt.
ARG TARGETARCH
ARG BWS_VERSION=2.1.0
ARG BWS_SHA256_ARM64=18253757286e119d450133a87eb463bf8c1ce418ce24c834f4f250d60cba6f9e
ARG BWS_SHA256_AMD64=ba8233c3a4aee5d43e3c73bbd04d99e9bc5aba13bbbfd06d89b073abe732b860
COPY install_bws.py /tmp/install_bws.py
RUN python /tmp/install_bws.py && rm /tmp/install_bws.py
COPY common.py components.py torrentfile.py trackers.py vault.py bridgelog.py puller.py bridge.py bridgeweb.py seedr_ftp.py reaper.py movierulz.py tamilmv.py youtube.py main.py /app/
COPY backends/ /app/backends/

CMD ["python", "-u", "main.py"]
