FROM python:3.9-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Build context is the repository root (see docker-compose.yml) so that the
# shared common.py can be copied into the image.
COPY video-scraper/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common.py ./
COPY video-scraper/app.py ./

CMD ["python", "app.py"]
