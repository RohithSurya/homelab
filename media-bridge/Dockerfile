FROM python:3.12-slim

WORKDIR /app
# 'docker' SDK lets the youtube component spawn disposable yt-dlp+ffmpeg containers
# via the host Docker socket (yt-dlp/ffmpeg stay out of this image).
RUN pip install --no-cache-dir requests docker

COPY common.py torrentfile.py bridge.py seedr_ftp.py reaper.py movierulz.py tamilmv.py daddylive.py youtube.py main.py /app/
COPY backends/ /app/backends/

CMD ["python", "-u", "main.py"]
