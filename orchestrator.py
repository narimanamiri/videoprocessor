#!/usr/bin/env python3
"""orchestrator - a small CLI to drive and observe the videoprocessor pipeline.

This is a thin, dependency-light client (standard library only) for operators.
It can:

  * ``submit``  - copy a local video into the watched input directory so the
                  pipeline picks it up, OR (with --direct) POST it straight to
                  the video-processor's /process endpoint.
  * ``status``  - query the /status and /health endpoints of every service.
  * ``health``  - quick health summary of all services.
  * ``run``     - submit a video then poll status until it finishes or times out.

Examples
--------
  # Drop a file into the input dir and let the watcher start the pipeline:
  python orchestrator.py submit ./myclip.mp4

  # Kick the processor directly (file must already be on a shared path):
  python orchestrator.py submit /app/processing/myclip.mp4 --direct

  # Show health of every service:
  python orchestrator.py health

  # Poll the processor's job status for a given file:
  python orchestrator.py status --service video-processor --name myclip.mp4

  # Submit and watch until done:
  python orchestrator.py run ./myclip.mp4 --timeout 600
"""

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

# Default service base URLs (override with env vars or --host/--port style args).
SERVICES = {
    "video-scraper": os.getenv("SCRAPER_URL", "http://localhost:5001"),
    "video-processor": os.getenv("PROCESSOR_URL", "http://localhost:5000"),
    "caption-generator": os.getenv("CAPTION_URL", "http://localhost:5002"),
    "ai-caption-agent": os.getenv("AI_CAPTION_URL", "http://localhost:5003"),
    "file-assembler": os.getenv("ASSEMBLER_URL", "http://localhost:5004"),
}

INPUT_DIR = os.getenv("HOST_INPUT_DIR", "shared-storage/input")


def _http_get(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return None, {"error": str(exc)}


def _http_post(url, payload, timeout=60):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, headers={"Content-Type": "application/json"}, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        try:
            body = json.loads(exc.read().decode("utf-8") or "{}")
        except Exception:  # noqa: BLE001
            body = {}
        return exc.code, body
    except Exception as exc:  # noqa: BLE001
        return None, {"error": str(exc)}


def cmd_health(_args):
    overall_ok = True
    for name, base in SERVICES.items():
        status, body = _http_get(f"{base}/health")
        ok = status == 200 and body.get("status") == "healthy"
        overall_ok = overall_ok and ok
        flag = "OK " if ok else "DOWN"
        print(f"[{flag}] {name:<18} {base}  ->  {body}")
    return 0 if overall_ok else 1


def cmd_status(args):
    targets = [args.service] if args.service else list(SERVICES)
    for name in targets:
        base = SERVICES.get(name)
        if not base:
            print(f"Unknown service: {name}", file=sys.stderr)
            continue
        url = f"{base}/status"
        if args.name and name in ("video-processor", "caption-generator"):
            url += f"?name={args.name}"
        status, body = _http_get(url)
        print(f"== {name} (HTTP {status}) ==")
        print(json.dumps(body, indent=2, ensure_ascii=False))
    return 0


def cmd_submit(args):
    src = Path(args.video)
    if args.direct:
        # POST straight to the processor. The path must be reachable by the
        # processor container (e.g. a shared-storage path inside /app).
        url = f"{SERVICES['video-processor']}/process"
        status, body = _http_post(url, {"video_path": str(src)})
        print(f"Submitted directly to processor (HTTP {status}):")
        print(json.dumps(body, indent=2, ensure_ascii=False))
        return 0 if status == 200 else 1

    if not src.exists():
        print(f"Error: file not found: {src}", file=sys.stderr)
        return 2
    input_dir = Path(args.input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    dest = input_dir / src.name
    shutil.copy2(src, dest)
    print(f"Copied {src} -> {dest}")
    print("The video-scraper will detect it and start the pipeline.")
    return 0


def cmd_run(args):
    rc = cmd_submit(args)
    if rc != 0:
        return rc

    name = Path(args.video).name
    deadline = time.time() + args.timeout
    print(f"Polling pipeline for '{name}' (timeout {args.timeout}s)...")
    last = None
    while time.time() < deadline:
        status, body = _http_get(
            f"{SERVICES['video-processor']}/status?name={name}"
        )
        state = body.get("status") if status == 200 else None
        if state != last:
            print(f"  processor: {state} ({body})")
            last = state
        if state in ("processed", "error"):
            break
        time.sleep(args.poll_interval)
    else:
        print("Timed out waiting for the pipeline.", file=sys.stderr)
        return 1
    return 0 if last == "processed" else 1


def build_parser():
    p = argparse.ArgumentParser(description="Drive the videoprocessor pipeline.")
    sub = p.add_subparsers(dest="command", required=True)

    sp = sub.add_parser("submit", help="Add a video to the pipeline.")
    sp.add_argument("video", help="Path to the video file.")
    sp.add_argument("--direct", action="store_true",
                    help="POST directly to the processor instead of copying "
                         "to the input dir.")
    sp.add_argument("--input-dir", default=INPUT_DIR,
                    help=f"Watched input dir (default: {INPUT_DIR}).")
    sp.set_defaults(func=cmd_submit)

    st = sub.add_parser("status", help="Show /status for services.")
    st.add_argument("--service", choices=list(SERVICES),
                    help="Limit to one service.")
    st.add_argument("--name", help="Job/video name to query.")
    st.set_defaults(func=cmd_status)

    he = sub.add_parser("health", help="Show /health for all services.")
    he.set_defaults(func=cmd_health)

    rn = sub.add_parser("run", help="Submit a video then poll until done.")
    rn.add_argument("video", help="Path to the video file.")
    rn.add_argument("--direct", action="store_true")
    rn.add_argument("--input-dir", default=INPUT_DIR)
    rn.add_argument("--timeout", type=int, default=600,
                    help="Seconds to wait before giving up (default 600).")
    rn.add_argument("--poll-interval", type=int, default=5,
                    help="Seconds between status polls (default 5).")
    rn.set_defaults(func=cmd_run)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
