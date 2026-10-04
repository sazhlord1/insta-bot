FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DATA_DIR=/data

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg ca-certificates \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
# yt-dlp is upgraded on every build because YouTube changes often
RUN pip install -r requirements.txt && pip install -U yt-dlp

COPY bot ./bot

CMD ["python", "-m", "bot.main"]
