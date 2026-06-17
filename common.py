"""Shared helpers for the videoprocessor microservices.

This module is intentionally dependency-light (only the standard library plus
``requests``, which every service already depends on) so it can be copied into
each service image without adding new packages.

It provides:
  * ``get_logger`` / ``configure_logging`` - structured (JSON-able) logging.
  * ``post_with_retry``                    - HTTP POST with exponential backoff.
  * ``env_int`` / ``env_float`` / ``env_bool`` - safe env-var parsing.
"""

import json
import logging
import os
import sys
import time

try:  # requests is available in every service image
    import requests
except Exception:  # pragma: no cover - defensive, keeps import side-effect free
    requests = None


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


def configure_logging(service_name):
    """Configure root logging once, honouring the ``LOG_LEVEL`` env var.

    When ``LOG_FORMAT=json`` (the default) records are emitted as JSON lines;
    set ``LOG_FORMAT=plain`` for human-friendly output.
    """
    level_name = os.getenv("LOG_LEVEL", "INFO").upper()
    level = getattr(logging, level_name, logging.INFO)

    handler = logging.StreamHandler(sys.stdout)
    if os.getenv("LOG_FORMAT", "json").lower() == "json":
        handler.setFormatter(_JsonFormatter())
    else:
        handler.setFormatter(
            logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s")
        )

    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level)

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
