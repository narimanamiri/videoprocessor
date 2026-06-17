# videoprocessor

A containerized, event-driven pipeline that ingests videos, processes them,
generates subtitles/captions (including AI captioning), extracts thumbnails and
metadata, and assembles the final output — orchestrated with **n8n** and Docker
Compose.

## Architecture

```
            drop file
               │
               ▼
   shared-storage/input ──watch──▶ video-scraper ──webhook──▶ n8n
                                        │ move
                                        ▼
                              shared-storage/processing
                                        │
        n8n routes the webhooks between the HTTP services below:
                                        │
  video-processor ─▶ caption-generator ─▶ ai-caption-agent ─▶ file-assembler
   (transcode +        (Whisper SRT +        (Ollama titles/      (collect into
    thumbnail +         transcript)           description/tags)    one folder)
    metadata)                                                          │
                                        ▼                              ▼
                              shared-storage/output ◀── final per-video folder
```

Services communicate over the `video-network` bridge and share data through
`./shared-storage` (`input` → `processing` → `output`). n8n (UI on port `5678`)
wires each service's webhook to the next service's HTTP endpoint.

### Services
| Service | Port | Endpoint(s) | Role |
|---------|------|-------------|------|
| `video-scraper` | `5001` | `/health`, `/status` | Watches the input dir, moves new videos to `processing`, notifies n8n |
| `video-processor` | `5000` | `/process`, `/status`, `/health` | Transcodes to Shorts (9:16) or Standard (16:9); extracts a thumbnail + metadata |
| `caption-generator` | `5002` | `/generate-subtitles`, `/status`, `/health` | Whisper-based SRT subtitles + transcript |
| `ai-caption-agent` | `5003` | `/generate-captions`, `/health` | AI titles/descriptions/tags via a local Ollama model (also `11434`) |
| `file-assembler` | `5004` | `/assemble-files`, `/health` | Collects video, thumbnail, subtitles and metadata into one output folder |
| `n8n` | `5678` | (UI) | Workflow orchestrator / webhooks |

## Requirements
- Docker and Docker Compose.
- Python 3 (only for the optional `orchestrator.py` CLI; standard library only).

## Quick start
```bash
docker compose up -d --build
```
- Drop videos into `shared-storage/input/` (or use the orchestrator CLI below).
- Open the n8n UI at <http://localhost:5678> to wire up / monitor workflows.
- Find results in `shared-storage/output/<video-name>/`.

---

## New features

### 1. Health & status endpoints on every service
Every service now exposes `GET /health`, and the processing services also expose
`GET /status`. Docker Compose includes a `healthcheck` per service so
`docker compose ps` shows real health, and `restart: unless-stopped` keeps them
running.

```bash
curl http://localhost:5000/health     # video-processor
curl http://localhost:5001/health     # video-scraper (now has an HTTP server)
curl http://localhost:5002/health     # caption-generator (reports Whisper model)
curl http://localhost:5003/health     # ai-caption-agent (reports Ollama model)
curl http://localhost:5004/health     # file-assembler

# Per-job processing status:
curl "http://localhost:5000/status?name=myclip.mp4"
curl http://localhost:5001/status      # scraper: detected/moved/error counts
```

### 2. Thumbnail + rich metadata extraction
`video-processor` extracts a JPEG thumbnail (`<name>_thumb.jpg`) and probes
width/height, codec, bitrate, container format, size and audio presence. Both
are carried through the pipeline and copied into the final output folder by
`file-assembler`. The `/process` response now includes a `metadata` block and a
`thumbnail_path`.

### 3. Resilient inter-service calls (retry + backoff) and structured logging
All outbound webhook/HTTP calls go through a shared helper that retries with
exponential backoff. All services emit single-line JSON logs for easy
aggregation. Both are env-tunable:

| Env var | Default | Meaning |
|---------|---------|---------|
| `HTTP_MAX_RETRIES` | `3` | Retries before giving up on a POST |
| `HTTP_BACKOFF_BASE` | `1.0` | Base backoff seconds (doubles each retry) |
| `HTTP_TIMEOUT` | `30` | Per-request timeout (seconds) |
| `LOG_LEVEL` | `INFO` | Standard Python log level |
| `LOG_FORMAT` | `json` | `json` or `plain` |

### 4. Env-var driven configuration
Behaviour is configurable without code changes (set in `docker-compose.yml` or
your environment):

| Service | Env var | Default | Meaning |
|---------|---------|---------|---------|
| video-processor | `SHORTS_DURATION_THRESHOLD` | `60` | Seconds at/under which a clip is a vertical Short |
| caption-generator | `WHISPER_MODEL` | `base` | `tiny`/`base`/`small`/`medium`/`large` |
| ai-caption-agent | `OLLAMA_API_HOST` | `http://localhost:11434` | Ollama API base URL |
| ai-caption-agent | `OLLAMA_MODEL` | `llama3.1:8b` | Generation model |
| ai-caption-agent | `OLLAMA_TEMPERATURE` | `0.7` | Sampling temperature |
| video-scraper | `FILE_STABLE_TIMEOUT` | `30` | Max seconds to wait for an upload to finish writing |
| video-scraper | `SCAN_INTERVAL` | `30` | Watcher loop interval |

### 5. Input-format validation
`video-processor` rejects unsupported inputs up front (returns HTTP 500 with a
clear message) instead of failing deep inside ffmpeg. Supported:
`.mp4 .avi .mov .mkv .wmv .flv .webm .m4v`.

### 6. Orchestrator CLI (`orchestrator.py`)
A dependency-light (standard-library-only) operator CLI to submit jobs and poll
status from your host:

```bash
# Health of every service
python orchestrator.py health

# Submit a local video into the watched input dir (scraper picks it up)
python orchestrator.py submit ./myclip.mp4

# Kick the processor directly with a shared-storage path
python orchestrator.py submit /app/processing/myclip.mp4 --direct

# Query status (optionally for one service / one job)
python orchestrator.py status
python orchestrator.py status --service video-processor --name myclip.mp4

# Submit then poll until the pipeline finishes (or times out)
python orchestrator.py run ./myclip.mp4 --timeout 600
```

Service URLs can be overridden with env vars (`PROCESSOR_URL`, `SCRAPER_URL`,
`CAPTION_URL`, `AI_CAPTION_URL`, `ASSEMBLER_URL`).

---

## Bug fixes included
- **video-processor**: duration is now read from the container `format` first,
  falling back to the video stream — previously many MP4s reported `None`
  duration and aborted.
- **caption-generator**: the temporary audio file is now unique per job (was a
  shared `temp_audio.wav`, causing a race when two videos ran concurrently) and
  is cleaned up in a `finally` block even on failure.
- **ai-caption-agent**: multi-paragraph `DESCRIPTION:` blocks are now captured
  in full (the old parser kept only the text on the marker line).
- **video-scraper**: waits for an upload to stop growing before moving it
  (replacing a brittle fixed `sleep(2)`), avoids clobbering same-named files,
  and bounds its in-memory `processed_files` set.
- **file-assembler**: folder names strip only the real video extension (and are
  sanitized against path traversal) instead of a buggy `str.replace`.
- Bare `except`/`print` paths replaced with structured logging and explicit
  error handling across all services.

## Development notes
- Each service image is built from the **repository root** as build context so
  the shared `common.py` can be included (see `dockerfile:` keys in
  `docker-compose.yml`).
- `shared-storage/{input,processing,output}` are kept in git via `.gitkeep`
  placeholders; their media contents are git-ignored.
