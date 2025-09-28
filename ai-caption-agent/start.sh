#!/bin/bash

# Start Ollama in the background
echo "Starting Ollama..."
ollama serve &

# Wait for Ollama to start
echo "Waiting for Ollama to start..."
sleep 10

# Pull the model (this will download it if not present)
echo "Pulling Llama 3.1 model..."
ollama pull llama3.1:8b

# Start the Python application
echo "Starting Python application..."
python app.py