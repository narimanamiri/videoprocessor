import os
import tempfile
import threading
from pathlib import Path

import ffmpeg
import whisper
from flask import Flask, request, jsonify, Response

from common import (
    get_logger,
    post_with_retry,
    env_int,
    Metrics,
    validate_video_input,
)

logger = get_logger("caption-generator")
app = Flask(__name__)
metrics = Metrics("caption-generator")
MAX_VIDEO_SIZE_MB = env_int("MAX_VIDEO_SIZE_MB", 0)


class CaptionGenerator:
    def __init__(self):
        # FEATURE: model size is configurable (tiny/base/small/medium/large).
        self.model_name = os.getenv("WHISPER_MODEL", "base")
        logger.info("Loading Whisper model: %s", self.model_name)
        self.model = whisper.load_model(self.model_name)
        self.model_loaded = True
        logger.info("Whisper model loaded successfully")

        self._jobs = {}
        self._jobs_lock = threading.Lock()

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

    def extract_audio(self, video_path, audio_path):
        """Extract mono 16kHz audio for transcription."""
        logger.info("Extracting audio from %s", video_path)
        (
            ffmpeg
            .input(str(video_path))
            .output(str(audio_path), format="wav", ac=1, ar="16000")
            .overwrite_output()
            .run(quiet=True)
        )
        logger.info("Audio extracted to %s", audio_path)

    def generate_subtitles(self, video_path, output_dir):
        video_path = Path(video_path)
        output_dir = Path(output_dir)
        name = video_path.name
        audio_path = None
        try:
            if not video_path.exists():
                raise FileNotFoundError(f"Video file not found: {video_path}")

            output_dir.mkdir(parents=True, exist_ok=True)
            self._set_status(name, "transcribing")

            # FEATURE/FIX: use a unique temp audio file per job so concurrent
            # requests can't clobber each other's "temp_audio.wav".
            fd, audio_name = tempfile.mkstemp(
                prefix=f"{video_path.stem}_", suffix=".wav", dir=str(output_dir)
            )
            os.close(fd)
            audio_path = Path(audio_name)

            self.extract_audio(video_path, audio_path)

            logger.info("Transcribing audio for %s ...", name)
            result = self.model.transcribe(str(audio_path))

            srt_path = output_dir / f"{video_path.stem}.srt"
            self.create_srt_file(result["segments"], srt_path)

            transcript_path = output_dir / f"{video_path.stem}_transcript.txt"
            self.create_transcript_file(result["text"], transcript_path)

            logger.info("Subtitles generated: %s", srt_path)
            metrics.inc("caption_subtitles_total")
            self._set_status(name, "subtitles_generated",
                             srt_path=str(srt_path))

            return {
                "srt_path": str(srt_path),
                "transcript_path": str(transcript_path),
                "transcript": result["text"],
                "language": result.get("language", "en"),
                "status": "subtitles_generated",
            }

        except Exception as exc:  # noqa: BLE001
            logger.exception("Error generating subtitles for %s", video_path)
            metrics.inc("caption_errors_total")
            self._set_status(name, "error", error=str(exc))
            return {"error": str(exc), "status": "error"}
        finally:
            # Always clean up the temp audio file, even on failure.
            if audio_path is not None:
                try:
                    audio_path.unlink(missing_ok=True)
                except OSError as exc:
                    logger.warning("Could not remove temp audio %s: %s",
                                   audio_path, exc)

    def create_srt_file(self, segments, srt_path):
        with open(srt_path, "w", encoding="utf-8") as f:
            for i, segment in enumerate(segments, 1):
                start = self.format_timestamp(segment["start"])
                end = self.format_timestamp(segment["end"])
                text = segment["text"].strip()
                f.write(f"{i}\n{start} --> {end}\n{text}\n\n")

    def create_transcript_file(self, transcript, transcript_path):
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript)

    def format_timestamp(self, seconds):
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        millis = int((secs - int(secs)) * 1000)
        secs = int(secs)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def send_to_next_step(self, result):
        url = os.getenv("N8N_WEBHOOK_URL",
                        "http://n8n:5678/webhook/subtitles-generated")
        response = post_with_retry(url, result, logger=logger)
        return response.status_code if response is not None else None


generator = CaptionGenerator()


@app.route("/generate-subtitles", methods=["POST"])
def generate_subtitles_endpoint():
    try:
        data = request.get_json(silent=True) or {}
        video_path = data.get("video_path")
        processed_path = data.get("processed_path")

        if not video_path and not processed_path:
            return jsonify({"error": "No video path provided"}), 400

        target_video_path = processed_path if processed_path else video_path

        ok, reason = validate_video_input(
            target_video_path, max_size_mb=MAX_VIDEO_SIZE_MB
        )
        if not ok:
            metrics.inc("caption_rejected_total")
            logger.warning("Rejected /generate-subtitles request: %s", reason)
            return jsonify({"error": reason, "status": "rejected"}), 400

        output_dir = os.getenv("OUTPUT_DIR", "/app/output")

        logger.info("Generating subtitles for: %s", target_video_path)
        result = generator.generate_subtitles(target_video_path, output_dir)

        if "error" in result:
            return jsonify(result), 500

        result.update(data)
        generator.send_to_next_step(result)
        return jsonify(result)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error in /generate-subtitles")
        return jsonify({"error": str(exc)}), 500


@app.route("/status", methods=["GET"])
def status_endpoint():
    name = request.args.get("name")
    if name:
        job = generator.get_status(name)
        if job is None:
            return jsonify({"error": "unknown job", "name": name}), 404
        return jsonify({"name": name, **job})
    return jsonify({"jobs": generator.get_status()})


@app.route("/metrics", methods=["GET"])
def metrics_endpoint():
    metrics.set_gauge("caption_jobs_tracked", len(generator.get_status()))
    return Response(metrics.render(), mimetype="text/plain; version=0.0.4")


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "caption-generator",
        "model": generator.model_name,
        "model_loaded": generator.model_loaded,
    })


def main():
    port = env_int("PORT", 5002)
    logger.info("Caption Generator started on port %s. Waiting for requests...",
                port)
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
