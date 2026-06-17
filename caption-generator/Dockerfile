FROM python:3.9-slim

WORKDIR /app

# Install system dependencies
RUN apt-get update && apt-get install -y \
    ffmpeg \
    git \
    build-essential \
    && rm -rf /var/lib/apt/lists/*

# Build context is the repository root (see docker-compose.yml).
COPY caption-generator/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common.py ./
COPY caption-generator/app.py ./

CMD ["python", "app.py"]
