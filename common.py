"""Shared helpers for the videoprocessor microservices.

This module is intentionally dependency-light (only the standard library plus
``requests``, which every service already depends on) so it can be copied into
each service image without adding new packages.

It provides:
  * ``get_logger`` / ``configure_logging`` - structured (JSON-able) logging.
  * ``post_with_retry``                    - HTTP POST with exponential backoff.
  * ``env_int`` / ``env_float`` / ``env_bool`` - safe env-var parsing.
  * ``Metrics``                            - tiny in-process counter registry that
                                             renders a Prometheus text endpoint.
  * ``validate_video_input``               - shared input validation / size limit.
"""

import json
import logging
import os
import sys
import threading
import time
from pathlib import Path

try:  # requests is available in every service image
    import requests
except Exception:  # pragma: no cover - defensive, keeps import side-effect free
    requests = None


# Video container extensions accepted across the pipeline. Kept here so every
# service validates against the same list (previously each service hard-coded a
# slightly different set, e.g. the scraper omitted ``.m4v``).
SUPPORTED_VIDEO_EXTENSIONS = {
    ".mp4", ".avi", ".mov", ".mkv", ".wmv", ".flv", ".webm", ".m4v",
}


# --------------------------------------------------------------------------- #
# Logging
# --------------------------------------------------------------------------- #
class _JsonFormatter(logging.Formatter):
    """Render log records as single-line JSON for easy aggregation."""

    def format(self, record):
        payload = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(record.created)),
            "level": record.levelname,
            "service": getattr(record, "service", None),
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            payload["exc"] = self.formatException(record.exc_info)
        # Attach any structured extras passed via ``extra={"extra_fields": {...}}``
        extra_fields = getattr(record, "extra_fields", None)
        if extra_fields:
            payload.update(extra_fields)
        return json.dumps(payload, ensure_ascii=False)


_LOGGING_CONFIGURED = False
_LOGGING_LOCK = threading.Lock()


def configure_logging(service_name):
    """Configure root logging once, honouring the ``LOG_LEVEL`` env var.

    When ``LOG_FORMAT=json`` (the default) records are emitted as JSON lines;
    set ``LOG_FORMAT=plain`` for human-friendly output.

    This is idempotent: the root handler is installed exactly once even if
    ``get_logger`` is called from several modules, so log lines are never
    duplicated.
    """
    global _LOGGING_CONFIGURED

    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    with _LOGGING_LOCK:
        if not _LOGGING_CONFIGURED:
            handler = logging.StreamHandler(sys.stdout)
            if os.getenv("LOG_FORMAT", "json").lower() == "json":
                handler.setFormatter(_JsonFormatter())
            else:
                handler.setFormatter(
                    logging.Formatter(
                        "%(asctime)s %(levelname)s [%(name)s] %(message)s"
                    )
                )
            root = logging.getLogger()
            root.handlers = [handler]
            root.setLevel(level)
            _LOGGING_CONFIGURED = True
        else:
            # Keep the level in sync if the env changed between calls.
            logging.getLogger().setLevel(level)

    logger = logging.getLogger(service_name)
    # Inject the service name into every record from this logger.
    logger = logging.LoggerAdapter(logger, {"service": service_name})
    return logger


def get_logger(service_name):
    """Return a configured logger for ``service_name``."""
    return configure_logging(service_name)


# --------------------------------------------------------------------------- #
# Env helpers
# --------------------------------------------------------------------------- #
def env_int(name, default):
    try:
        return int(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_float(name, default):
    try:
        return float(os.getenv(name, str(default)))
    except (TypeError, ValueError):
        return default


def env_bool(name, default=False):
    val = os.getenv(name)
    if val is None:
        return default
    return val.strip().lower() in ("1", "true", "yes", "on")


def human_size(num_bytes):
    """Render a byte count as a short human-readable string (e.g. ``17.0 MB``)."""
    if num_bytes is None:
        return "unknown"
    size = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}" if unit != "B" else f"{int(size)} B"
        size /= 1024


# --------------------------------------------------------------------------- #
# Input validation / size limits
# --------------------------------------------------------------------------- #
def validate_video_input(video_path, max_size_mb=None, extensions=None):
    """Validate a video path before expensive ffmpeg/Whisper work begins.

    Checks, in order:
      * the path is non-empty,
      * the file exists on disk,
      * the extension is one of ``extensions`` (default: the shared set),
      * the file is not larger than ``max_size_mb`` (env ``MAX_VIDEO_SIZE_MB``,
        default 0 = unlimited).

    Returns ``(True, None)`` when valid, otherwise ``(False, reason)`` so the
    caller can return a clear HTTP error instead of crashing deep inside ffmpeg.
    """
    if not video_path:
        return False, "no video_path provided"

    if extensions is None:
        extensions = SUPPORTED_VIDEO_EXTENSIONS
    if max_size_mb is None:
        max_size_mb = env_int("MAX_VIDEO_SIZE_MB", 0)

    path = Path(video_path)
    if not path.exists():
        return False, f"file not found: {path}"

    suffix = path.suffix.lower()
    if suffix not in extensions:
        return False, (
            f"unsupported input format '{path.suffix}'; "
            f"supported: {sorted(extensions)}"
        )

    if max_size_mb and max_size_mb > 0:
        try:
            size_bytes = path.stat().st_size
        except OSError as exc:
            return False, f"could not stat file: {exc}"
        limit_bytes = max_size_mb * 1024 * 1024
        if size_bytes > limit_bytes:
            return False, (
                f"file too large: {human_size(size_bytes)} exceeds limit "
                f"{max_size_mb} MB"
            )

    return True, None


# --------------------------------------------------------------------------- #
# Lightweight in-process metrics (Prometheus text exposition)
# --------------------------------------------------------------------------- #
class Metrics:
    """A tiny, thread-safe counter/gauge registry with Prometheus rendering.

    Deliberately minimal (no external client library) so it can ship in every
    service image. Use ``inc`` for monotonically increasing counters and
    ``set_gauge`` for point-in-time values, then expose ``render()`` from a
    ``/metrics`` route.
    """

    def __init__(self, service):
        self.service = service
        self._lock = threading.Lock()
        self._counters = {}
        self._gauges = {}
        self._started = time.time()

    def inc(self, name, amount=1):
        with self._lock:
            self._counters[name] = self._counters.get(name, 0) + amount

    def set_gauge(self, name, value):
        with self._lock:
            self._gauges[name] = value

    def snapshot(self):
        with self._lock:
            return {
                "counters": dict(self._counters),
                "gauges": dict(self._gauges),
                "uptime_seconds": time.time() - self._started,
            }

    def render(self):
        """Return Prometheus text-exposition formatted metrics."""
        lines = []
        label = f'{{service="{self.service}"}}'
        with self._lock:
            lines.append("# TYPE videoprocessor_uptime_seconds gauge")
            lines.append(
                f"videoprocessor_uptime_seconds{label} "
                f"{time.time() - self._started:.1f}"
            )
            for name, value in sorted(self._counters.items()):
                metric = f"videoprocessor_{name}"
                lines.append(f"# TYPE {metric} counter")
                lines.append(f"{metric}{label} {value}")
            for name, value in sorted(self._gauges.items()):
                metric = f"videoprocessor_{name}"
                lines.append(f"# TYPE {metric} gauge")
                lines.append(f"{metric}{label} {value}")
        return "\n".join(lines) + "\n"


# --------------------------------------------------------------------------- #
# Resilient HTTP
# --------------------------------------------------------------------------- #
def post_with_retry(url, payload, logger=None, max_retries=None,
                    backoff=None, timeout=None):
    """POST ``payload`` as JSON to ``url`` with exponential backoff.

    Configurable via env vars (with explicit-arg overrides):
      * ``HTTP_MAX_RETRIES`` (default 3)
      * ``HTTP_BACKOFF_BASE`` seconds (default 1.0)
      * ``HTTP_TIMEOUT``      seconds (default 30)

    Returns the ``requests.Response`` on success, or ``None`` if all attempts
    fail. Never raises on network errors - callers treat ``None`` as failure.
    """
    if requests is None:
        if logger:
            logger.error("requests library unavailable; cannot POST to %s", url)
        return None

    if max_retries is None:
        max_retries = env_int("HTTP_MAX_RETRIES", 3)
    if backoff is None:
        backoff = env_float("HTTP_BACKOFF_BASE", 1.0)
    if timeout is None:
        timeout = env_float("HTTP_TIMEOUT", 30.0)

    attempt = 0
    while True:
        attempt += 1
        try:
            response = requests.post(url, json=payload, timeout=timeout)
            response.raise_for_status()
            if logger:
                logger.info("POST %s succeeded (status=%s, attempt=%s)",
                            url, response.status_code, attempt)
            return response
        except Exception as exc:  # noqa: BLE001 - we want to retry on any failure
            if attempt > max_retries:
                if logger:
                    logger.error("POST %s failed after %s attempts: %s",
                                 url, attempt, exc)
                return None
            sleep_for = backoff * (2 ** (attempt - 1))
            if logger:
                logger.warning("POST %s failed (attempt=%s/%s): %s; retrying in %.1fs",
                               url, attempt, max_retries, exc, sleep_for)
            time.sleep(sleep_for)
