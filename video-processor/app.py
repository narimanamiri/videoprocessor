import os
import threading
from pathlib import Path

import ffmpeg
from flask import Flask, request, jsonify

from common import get_logger, post_with_retry, env_int

logger = get_logger("video-processor")
app = Flask(__name__)

# Formats we are willing to transcode. Anything else is rejected up front
# instead of failing deep inside ffmpeg with a cryptic error.
SUPPORTED_INPUT_FORMATS = {
    ".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm", ".m4v",
}

# Threshold (seconds) under which a video is treated as a vertical "short".
SHORTS_THRESHOLD = env_int("SHORTS_DURATION_THRESHOLD", 60)


class VideoProcessor:
    def __init__(self, processing_dir, output_dir, n8n_webhook_url):
        self.processing_dir = Path(processing_dir)
        self.output_dir = Path(output_dir)
        self.n8n_webhook_url = n8n_webhook_url

        self.output_dir.mkdir(parents=True, exist_ok=True)

        # In-memory job status registry (FEATURE: /status endpoint).
        self._jobs = {}
        self._jobs_lock = threading.Lock()

    # --------------------------------------------------------------------- #
    # Status tracking
    # --------------------------------------------------------------------- #
    def _set_status(self, name, status, **extra):
        with self._jobs_lock:
            job = self._jobs.setdefault(name, {})
            job["status"] = status
            job.update(extra)

    def get_status(self, name=None):
        with self._jobs_lock:
            if name is not None:
                return self._jobs.get(name)
            return dict(self._jobs)

    # --------------------------------------------------------------------- #
    # Probing / metadata
    # --------------------------------------------------------------------- #
    def probe_metadata(self, video_path):
        """Return rich metadata for ``video_path`` using a single ffprobe call.

        Duration is read from the container ``format`` first (reliable across
        codecs) and only falls back to the video stream. This fixes a bug where
        MP4s that don't carry per-stream duration reported ``None`` and aborted
        processing.
        """
        probe = ffmpeg.probe(str(video_path))
        fmt = probe.get("format", {})
        streams = probe.get("streams", [])

        video_stream = next(
            (s for s in streams if s.get("codec_type") == "video"), None
        )
        audio_stream = next(
            (s for s in streams if s.get("codec_type") == "audio"), None
        )

        duration = None
        for source in (fmt.get("duration"),
                       video_stream.get("duration") if video_stream else None):
            try:
                if source is not None:
                    duration = float(source)
                    break
            except (TypeError, ValueError):
                continue

        metadata = {
            "duration": duration,
            "size_bytes": int(fmt["size"]) if fmt.get("size") else None,
            "format_name": fmt.get("format_name"),
            "bit_rate": int(fmt["bit_rate"]) if fmt.get("bit_rate") else None,
            "has_audio": audio_stream is not None,
        }
        if video_stream is not None:
            metadata.update({
                "width": video_stream.get("width"),
                "height": video_stream.get("height"),
                "video_codec": video_stream.get("codec_name"),
            })
        return metadata

    def extract_thumbnail(self, video_path, output_path, at_seconds=1.0):
        """Grab a single representative frame as a JPEG thumbnail."""
        try:
            (
                ffmpeg
                .input(str(video_path), ss=at_seconds)
                .output(str(output_path), vframes=1, **{"q:v": 2})
                .overwrite_output()
                .run(quiet=True)
            )
            return True
        except Exception as exc:  # noqa: BLE001
            logger.warning("Thumbnail extraction failed for %s: %s",
                           video_path, exc)
            return False

    # --------------------------------------------------------------------- #
    # Processing
    # --------------------------------------------------------------------- #
    def process_video(self, video_path):
        video_path = Path(video_path)
        name = video_path.name
        try:
            if not video_path.exists():
                raise FileNotFoundError(f"Video file not found: {video_path}")

            if video_path.suffix.lower() not in SUPPORTED_INPUT_FORMATS:
                raise ValueError(
                    f"Unsupported input format '{video_path.suffix}'. "
                    f"Supported: {sorted(SUPPORTED_INPUT_FORMATS)}"
                )

            self._set_status(name, "processing")
            metadata = self.probe_metadata(video_path)
            duration = metadata.get("duration")

            if duration is None:
                raise ValueError("Could not determine video duration")

            is_short = duration <= SHORTS_THRESHOLD

            output_filename = f"processed_{video_path.stem}.mp4"
            output_path = self.output_dir / output_filename

            if is_short:
                self.process_shorts(video_path, output_path)
                video_type = "shorts"
            else:
                self.process_standard(video_path, output_path)
                video_type = "standard"

            # FEATURE: thumbnail extracted alongside the processed video.
            thumbnail_path = self.output_dir / f"{video_path.stem}_thumb.jpg"
            has_thumb = self.extract_thumbnail(
                video_path, thumbnail_path, at_seconds=min(1.0, duration / 2)
            )

            result = {
                "original_path": str(video_path),
                "processed_path": str(output_path),
                "thumbnail_path": str(thumbnail_path) if has_thumb else None,
                "duration": duration,
                "video_type": video_type,
                "output_filename": output_filename,
                "metadata": metadata,
                "status": "processed",
            }
            self._set_status(name, "processed",
                             output=str(output_path), video_type=video_type)
            logger.info("Processed %s as %s (%.1fs)",
                        name, video_type, duration)
            return result

        except Exception as exc:  # noqa: BLE001
            logger.exception("Error processing video %s", video_path)
            self._set_status(name, "error", error=str(exc))
            return {"error": str(exc), "status": "error"}

    def process_shorts(self, input_path, output_path):
        logger.info("Processing as Shorts: %s -> %s", input_path, output_path)
        (
            ffmpeg
            .input(str(input_path))
            .filter("scale", 1080, 1920)
            .filter("pad", 1080, 1920, -1, -1, "black")
            .output(str(output_path), vcodec="libx264", acodec="aac",
                    **{"b:v": "2M", "b:a": "192k"})
            .overwrite_output()
            .run(quiet=True)
        )

    def process_standard(self, input_path, output_path):
        logger.info("Processing as Standard: %s -> %s", input_path, output_path)
        (
            ffmpeg
            .input(str(input_path))
            .filter("scale", 1920, 1080)
            .output(str(output_path), vcodec="libx264", acodec="aac",
                    **{"b:v": "5M", "b:a": "192k"})
            .overwrite_output()
            .run(quiet=True)
        )

    def send_to_next_step(self, result):
        response = post_with_retry(self.n8n_webhook_url, result, logger=logger)
        return response.status_code if response is not None else None


processor = VideoProcessor(
    processing_dir=os.getenv("PROCESSING_DIR", "/app/processing"),
    output_dir=os.getenv("OUTPUT_DIR", "/app/output"),
    n8n_webhook_url=os.getenv("N8N_WEBHOOK_URL",
                              "http://n8n:5678/webhook/video-processed"),
)


@app.route("/process", methods=["POST"])
def process_video_endpoint():
    try:
        data = request.get_json(silent=True) or {}
        video_path = data.get("video_path")

        if not video_path:
            return jsonify({"error": "No video_path provided"}), 400

        logger.info("Processing request for: %s", video_path)
        result = processor.process_video(video_path)

        if "error" in result:
            return jsonify(result), 500

        processor.send_to_next_step(result)
        return jsonify(result)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error in /process")
        return jsonify({"error": str(exc)}), 500


@app.route("/status", methods=["GET"])
def status_endpoint():
    """Return processing status for all jobs, or one named via ?name=."""
    name = request.args.get("name")
    if name:
        job = processor.get_status(name)
        if job is None:
            return jsonify({"error": "unknown job", "name": name}), 404
        return jsonify({"name": name, **job})
    return jsonify({"jobs": processor.get_status()})


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "service": "video-processor"})


def main():
    port = env_int("PORT", 5000)
    logger.info("Video Processor started on port %s. Waiting for requests...",
                port)
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
