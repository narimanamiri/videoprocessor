import os
import threading
import time
from pathlib import Path

from flask import Flask, jsonify, Response
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

from common import (
    get_logger,
    post_with_retry,
    env_int,
    env_float,
    Metrics,
    SUPPORTED_VIDEO_EXTENSIONS,
)

logger = get_logger("video-scraper")
app = Flask(__name__)
metrics = Metrics("video-scraper")

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
        # Use the shared extension set so the scraper, processor and assembler
        # all agree on what a "video" is (previously ``.m4v`` was missing here).
        self.supported_formats = set(SUPPORTED_VIDEO_EXTENSIONS)
        self.processed_files = set()
        self._lock = threading.Lock()

        # How long to wait for a file to stop growing before we treat it as
        # fully written (configurable; fixes the fixed 2s race condition).
        self.stable_timeout = env_float("FILE_STABLE_TIMEOUT", 30.0)
        self.stable_interval = env_float("FILE_STABLE_INTERVAL", 1.0)

    def on_created(self, event):
        if event.is_directory:
            return
        self._maybe_handle(Path(event.src_path))

    def on_moved(self, event):
        # Some upload flows write to a temp name then rename into place, which
        # surfaces as a move (not a create). Handle the destination too.
        if event.is_directory:
            return
        self._maybe_handle(Path(event.dest_path))

    def _maybe_handle(self, file_path):
        if file_path.suffix.lower() not in self.supported_formats:
            return
        with self._lock:
            if file_path.name in self.processed_files:
                return
            # Reserve the name immediately so a duplicate create/move event for
            # the same file can't kick off a second concurrent move.
            self.processed_files.add(file_path.name)
        logger.info("New video detected: %s", file_path)
        metrics.inc("scraper_detected_total")
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
                metrics.inc("scraper_errors_total")
                _record("errors",
                        {"name": video_path.name, "reason": "not_stable"})
                # Un-reserve so a later, complete copy of the same name can be
                # picked up instead of being silently ignored forever.
                self._forget(video_path.name)
                return

            # Avoid clobbering an existing file of the same name in processing.
            if processing_path.exists():
                stem, suffix = video_path.stem, video_path.suffix
                processing_path = self.processing_dir / f"{stem}_{int(time.time())}{suffix}"

            video_path.replace(processing_path)
            self._bound_processed_set()

            logger.info("Moved video to processing: %s", processing_path)
            metrics.inc("scraper_moved_total")
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
            metrics.inc("scraper_errors_total")
            _record("errors", {"name": video_path.name, "reason": str(exc)})
            self._forget(video_path.name)

    def _forget(self, name):
        with self._lock:
            self.processed_files.discard(name)

    def _bound_processed_set(self):
        with self._lock:
            if len(self.processed_files) > 500:
                # Bound memory: keep the set from growing unbounded.
                self.processed_files = set(list(self.processed_files)[-500:])

    def scan_existing(self, input_dir):
        """Pick up videos that already exist at startup.

        Watchdog only fires for filesystem events that happen *after* the
        observer starts, so files dropped while the service was down would
        otherwise be ignored until touched again.
        """
        input_path = Path(input_dir)
        if not input_path.is_dir():
            return
        for entry in sorted(input_path.iterdir()):
            if entry.is_file() and entry.suffix.lower() in self.supported_formats:
                self._maybe_handle(entry)


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


@app.route("/metrics", methods=["GET"])
def metrics_endpoint():
    with _state_lock:
        metrics.set_gauge("scraper_errors_current", len(_state["errors"]))
    return Response(metrics.render(), mimetype="text/plain; version=0.0.4")


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

    # Pick up any files already sitting in the input dir at startup.
    event_handler.scan_existing(input_dir)

    try:
        while True:
            time.sleep(scan_interval)
    except KeyboardInterrupt:
        logger.info("Shutting down video scraper")
        observer.stop()
    observer.join()


if __name__ == "__main__":
    main()
