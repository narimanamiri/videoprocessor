import os
import threading
import time
from pathlib import Path

from flask import Flask, jsonify
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from common import get_logger, post_with_retry, env_int, env_float

logger = get_logger("video-scraper")
app = Flask(__name__)

# Shared, thread-safe record of what the scraper has done (FEATURE: /status).
_state_lock = threading.Lock()
_state = {"detected": [], "moved": [], "errors": []}


def _record(bucket, item):
    with _state_lock:
        _state[bucket].append(item)
        # Keep the in-memory history bounded.
        if len(_state[bucket]) > 200:
            _state[bucket] = _state[bucket][-200:]


class VideoHandler(FileSystemEventHandler):
    def __init__(self, processing_dir, n8n_webhook_url):
        self.processing_dir = Path(processing_dir)
        self.n8n_webhook_url = n8n_webhook_url
        self.supported_formats = {
            ".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm",
        }
        self.processed_files = set()
        self._lock = threading.Lock()

        # How long to wait for a file to stop growing before we treat it as
        # fully written (configurable; fixes the fixed 2s race condition).
        self.stable_timeout = env_float("FILE_STABLE_TIMEOUT", 30.0)
        self.stable_interval = env_float("FILE_STABLE_INTERVAL", 1.0)

    def on_created(self, event):
        if event.is_directory:
            return
        file_path = Path(event.src_path)
        with self._lock:
            already = file_path.name in self.processed_files
        if file_path.suffix.lower() in self.supported_formats and not already:
            logger.info("New video detected: %s", file_path)
            _record("detected", {"name": file_path.name, "ts": time.time()})
            self.process_video(file_path)

    def _wait_until_stable(self, file_path):
        """Block until ``file_path`` stops growing or the timeout elapses.

        Returns True if the file reached a stable size, False on timeout or if
        the file disappeared. This replaces a brittle fixed ``sleep(2)`` that
        could move half-written uploads.
        """
        deadline = time.time() + self.stable_timeout
        last_size = -1
        while time.time() < deadline:
            try:
                size = file_path.stat().st_size
            except FileNotFoundError:
                return False
            if size == last_size and size > 0:
                return True
            last_size = size
            time.sleep(self.stable_interval)
        return False

    def process_video(self, video_path):
        processing_path = self.processing_dir / video_path.name
        try:
            if not self._wait_until_stable(video_path):
                logger.warning("File %s did not stabilize in %.0fs; skipping",
                               video_path, self.stable_timeout)
                _record("errors",
                        {"name": video_path.name, "reason": "not_stable"})
                return

            # Avoid clobbering an existing file of the same name in processing.
            if processing_path.exists():
                stem, suffix = video_path.stem, video_path.suffix
                processing_path = self.processing_dir / f"{stem}_{int(time.time())}{suffix}"

            video_path.replace(processing_path)
            with self._lock:
                self.processed_files.add(video_path.name)
                if len(self.processed_files) > 500:
                    # Bound memory: keep the set from growing unbounded.
                    self.processed_files = set(list(self.processed_files)[-500:])

            logger.info("Moved video to processing: %s", processing_path)
            _record("moved", {"name": processing_path.name, "ts": time.time()})

            payload = {
                "video_path": str(processing_path),
                "video_name": processing_path.name,
                "timestamp": time.time(),
                "status": "detected",
            }
            post_with_retry(self.n8n_webhook_url, payload, logger=logger)

        except Exception as exc:  # noqa: BLE001
            logger.exception("Error processing video %s", video_path)
            _record("errors", {"name": video_path.name, "reason": str(exc)})


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "service": "video-scraper"})


@app.route("/status", methods=["GET"])
def status_endpoint():
    with _state_lock:
        snapshot = {
            "detected_count": len(_state["detected"]),
            "moved_count": len(_state["moved"]),
            "error_count": len(_state["errors"]),
            "recent_moved": _state["moved"][-10:],
            "recent_errors": _state["errors"][-10:],
        }
    return jsonify(snapshot)


def _run_http_server(port):
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)


def main():
    input_dir = os.getenv("INPUT_DIR", "/app/input")
    processing_dir = os.getenv("PROCESSING_DIR", "/app/processing")
    n8n_webhook_url = os.getenv(
        "N8N_WEBHOOK_URL", "http://n8n:5678/webhook/video-detected"
    )
    scan_interval = env_int("SCAN_INTERVAL", 30)
    http_port = env_int("PORT", 5001)

    Path(input_dir).mkdir(parents=True, exist_ok=True)
    Path(processing_dir).mkdir(parents=True, exist_ok=True)

    logger.info("Video Scraper config: input=%s processing=%s webhook=%s",
                input_dir, processing_dir, n8n_webhook_url)

    # FEATURE: lightweight HTTP server for /health and /status, run in a
    # daemon thread alongside the filesystem watcher.
    http_thread = threading.Thread(
        target=_run_http_server, args=(http_port,), daemon=True
    )
    http_thread.start()
    logger.info("Health/status server listening on port %s", http_port)

    event_handler = VideoHandler(processing_dir, n8n_webhook_url)
    observer = Observer()
    observer.schedule(event_handler, input_dir, recursive=False)

    logger.info("Starting video scraper. Watching: %s", input_dir)
    observer.start()

    try:
        while True:
            time.sleep(scan_interval)
    except KeyboardInterrupt:
        logger.info("Shutting down video scraper")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
