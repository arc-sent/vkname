FROM python:3.10-slim

# ffmpeg нужен yt-dlp для склейки видео и аудио
RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py db.py downloader.py ./

ENV DATA_DIR=/app/data
VOLUME ["/app/data"]

CMD ["python", "bot.py"]
