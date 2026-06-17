FROM python:3.9-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# Build context is the repository root (see docker-compose.yml).
COPY video-processor/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common.py ./
COPY video-processor/app.py ./

CMD ["python", "app.py"]
