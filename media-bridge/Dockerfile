FROM python:3.12-slim

WORKDIR /app
# 'docker' SDK lets the youtube component spawn disposable yt-dlp+ffmpeg containers
# via the host Docker socket (yt-dlp/ffmpeg stay out of this image).
RUN pip install --no-cache-dir requests docker

COPY common.py seedr.py reaper.py movierulz.py tamilmv.py daddylive.py youtube.py main.py /app/

CMD ["python", "-u", "main.py"]
