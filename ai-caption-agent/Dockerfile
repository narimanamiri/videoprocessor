FROM python:3.9-slim

WORKDIR /app

# Install system dependencies including curl for Ollama installation
RUN apt-get update && apt-get install -y \
    curl \
    && rm -rf /var/lib/apt/lists/*

# Install Ollama
RUN curl -fsSL https://ollama.ai/install.sh | sh

# Install Python dependencies
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py start.sh ./

# Make start script executable
RUN chmod +x start.sh

# Create models directory
RUN mkdir -p /app/models

CMD ["./start.sh"]