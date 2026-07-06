FROM python:3.12-slim

WORKDIR /app
RUN pip install --no-cache-dir requests

COPY common.py seedr.py reaper.py movierulz.py daddylive.py main.py /app/

CMD ["python", "-u", "main.py"]
