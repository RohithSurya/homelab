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
# Bitwarden Secrets Manager CLI (vault.py). Pinned + checksum-verified release asset.
ARG BWS_VERSION=2.1.0
ARG BWS_SHA256=18253757286e119d450133a87eb463bf8c1ce418ce24c834f4f250d60cba6f9e
RUN python - <<'PY'
import hashlib, io, os, urllib.request, zipfile
v, want = os.environ["BWS_VERSION"], os.environ["BWS_SHA256"]
url = f"https://github.com/bitwarden/sdk-sm/releases/download/bws-v{v}/bws-aarch64-unknown-linux-gnu-{v}.zip"
data = urllib.request.urlopen(url, timeout=120).read()
got = hashlib.sha256(data).hexdigest()
assert got == want, f"bws checksum mismatch: {got}"
zipfile.ZipFile(io.BytesIO(data)).extract("bws", "/usr/local/bin")
os.chmod("/usr/local/bin/bws", 0o755)
PY

COPY common.py torrentfile.py trackers.py vault.py bridgelog.py puller.py bridge.py bridgeweb.py seedr_ftp.py reaper.py movierulz.py tamilmv.py daddylive.py youtube.py main.py /app/
COPY backends/ /app/backends/

CMD ["python", "-u", "main.py"]
