# videoprocessor

A containerized, event-driven pipeline that ingests videos, processes them,
generates subtitles/captions (including AI captioning), and assembles the final
output — orchestrated with **n8n** and Docker Compose.

## Services
| Service | Role |
|---------|------|
| `video-scraper` | Watches an input dir for new videos and notifies n8n |
| `video-processor` | Processes/transcodes detected videos |
| `caption-generator` | Generates subtitles |
| `ai-caption-agent` | AI captioning via a local Ollama model (port `11434`) |
| `file-assembler` | Assembles processed media + captions into final files |
| `n8n` | Workflow orchestrator / webhooks (UI on port `5678`) |

Services communicate over the `video-network` bridge and share data through
`./shared-storage` (`input` → `processing` → `output`).

## Requirements
- Docker and Docker Compose.

## Usage
```bash
docker compose up -d --build
```
- Drop videos into `shared-storage/input/`.
- Open the n8n UI at <http://localhost:5678> to wire up / monitor workflows.
- Find results in `shared-storage/output/`.
