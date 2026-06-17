FROM python:3.9-slim

WORKDIR /app

# Build context is the repository root (see docker-compose.yml).
COPY file-assembler/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY common.py ./
COPY file-assembler/app.py ./

CMD ["python", "app.py"]
