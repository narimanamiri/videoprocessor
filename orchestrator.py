#!/usr/bin/env python3
"""orchestrator - a small CLI to drive and observe the videoprocessor pipeline.

This is a thin, dependency-light client (standard library only) for operators.
It can:

  * ``submit``     - copy a local video into the watched input directory so the
                     pipeline picks it up, OR (with --direct) POST it straight to
                     the video-processor's /process endpoint.
  * ``status``     - query the /status and /health endpoints of every service.
  * ``health``     - quick health summary of all services.
  * ``run``        - submit a video then poll status until it finishes/times out.
                     Failures are recorded to the dead-letter folder.
  * ``dashboard``  - aggregated health + metrics for every service; ``--html``
                     writes a self-contained static dashboard page.
  * ``dead-letter``- list jobs that ended in the dead-letter folder.
  * ``requeue``    - re-submit dead-lettered jobs back into the pipeline.

Examples
--------
  # Drop a file into the input dir and let the watcher start the pipeline:
  python orchestrator.py submit ./myclip.mp4

  # Kick the processor directly (file must already be on a shared path):
  python orchestrator.py submit /app/processing/myclip.mp4 --direct

  # Show health of every service:
  python orchestrator.py health

  # Aggregated dashboard (text), or write an HTML page:
  python orchestrator.py dashboard
  python orchestrator.py dashboard --html status.html

  # Poll the processor's job status for a given file:
  python orchestrator.py status --service video-processor --name myclip.mp4

  # Submit and watch until done (records failures to the dead-letter folder):
  python orchestrator.py run ./myclip.mp4 --timeout 600

  # Inspect and retry failed jobs:
  python orchestrator.py dead-letter
  python orchestrator.py requeue --all
"""

import argparse
import json
import os
import shutil
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

# Default service base URLs (override with env vars or --host/--port style args).
SERVICES = {
    "video-scraper": os.getenv("SCRAPER_URL", "http://localhost:5001"),
    "video-processor": os.getenv("PROCESSOR_URL", "http://localhost:5000"),
    "caption-generator": os.getenv("CAPTION_URL", "http://localhost:5002"),
    "ai-caption-agent": os.getenv("AI_CAPTION_URL", "http://localhost:5003"),
    "file-assembler": os.getenv("ASSEMBLER_URL", "http://localhost:5004"),
}

# Services that expose a per-job /status endpoint.
STATUS_SERVICES = ("video-processor", "caption-generator", "video-scraper")

INPUT_DIR = os.getenv("HOST_INPUT_DIR", "shared-storage/input")
# Dead-letter folder for jobs that failed/timed out (FEATURE).
DEAD_LETTER_DIR = os.getenv("DEAD_LETTER_DIR", "shared-storage/failed")


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


def _now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# Dead-letter queue (failed/timed-out jobs)
# --------------------------------------------------------------------------- #
def _dead_letter_dir():
    path = Path(DEAD_LETTER_DIR)
    path.mkdir(parents=True, exist_ok=True)
    return path


def record_dead_letter(video_path, reason, extra=None):
    """Persist a small JSON record describing a failed/timed-out job.

    The record keeps the original source path so ``requeue`` can re-submit it.
    """
    dl_dir = _dead_letter_dir()
    src = Path(video_path)
    record = {
        "name": src.name,
        "source_path": str(src.resolve()) if src.exists() else str(src),
        "reason": reason,
        "recorded_at": _now_iso(),
    }
    if extra:
        record["details"] = extra
    # One record per name; newest wins. Suffix is .json so it's easy to scan.
    target = dl_dir / f"{src.stem}.json"
    with open(target, "w", encoding="utf-8") as f:
        json.dump(record, f, indent=2, ensure_ascii=False)
    print(f"Recorded dead-letter: {target}")
    return target


def load_dead_letters():
    dl_dir = Path(DEAD_LETTER_DIR)
    if not dl_dir.is_dir():
        return []
    records = []
    for entry in sorted(dl_dir.glob("*.json")):
        try:
            with open(entry, "r", encoding="utf-8") as f:
                rec = json.load(f)
        except (OSError, json.JSONDecodeError) as exc:
            print(f"Skipping unreadable dead-letter {entry}: {exc}",
                  file=sys.stderr)
            continue
        rec["_record_file"] = str(entry)
        records.append(rec)
    return records


def cmd_dead_letter(_args):
    records = load_dead_letters()
    if not records:
        print(f"No dead-letter records in {DEAD_LETTER_DIR}/")
        return 0
    print(f"Dead-letter jobs in {DEAD_LETTER_DIR}/ ({len(records)}):")
    for rec in records:
        print(f"  - {rec.get('name'):<30} {rec.get('reason')}  "
              f"[{rec.get('recorded_at')}]")
        print(f"      source: {rec.get('source_path')}")
    return 0


def cmd_requeue(args):
    records = load_dead_letters()
    if not records:
        print(f"Nothing to requeue in {DEAD_LETTER_DIR}/")
        return 0

    if not args.all and not args.name:
        print("Specify --all or --name <video> to requeue.", file=sys.stderr)
        return 2

    selected = records if args.all else [
        r for r in records if r.get("name") == args.name
        or Path(r.get("name", "")).stem == args.name
    ]
    if not selected:
        print(f"No dead-letter record matches '{args.name}'.", file=sys.stderr)
        return 1

    input_dir = Path(args.input_dir)
    input_dir.mkdir(parents=True, exist_ok=True)
    failures = 0
    for rec in selected:
        src = Path(rec.get("source_path", ""))
        if not src.exists():
            print(f"  SKIP {rec.get('name')}: source missing ({src})",
                  file=sys.stderr)
            failures += 1
            continue
        dest = input_dir / src.name
        shutil.copy2(src, dest)
        print(f"  Requeued {src.name} -> {dest}")
        # Clear the dead-letter record now that it's back in the pipeline.
        try:
            os.remove(rec["_record_file"])
        except OSError:
            pass
    return 1 if failures else 0


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
        if args.name and name in STATUS_SERVICES:
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
    last_body = {}
    while time.time() < deadline:
        status, body = _http_get(
            f"{SERVICES['video-processor']}/status?name={name}"
        )
        state = body.get("status") if status == 200 else None
        if state != last:
            print(f"  processor: {state} ({body})")
            last = state
            last_body = body
        if state in ("processed", "error"):
            break
        time.sleep(args.poll_interval)
    else:
        # Timed out: record to the dead-letter folder unless disabled.
        print("Timed out waiting for the pipeline.", file=sys.stderr)
        if not args.no_dead_letter:
            record_dead_letter(args.video, "timeout",
                               extra={"timeout_seconds": args.timeout})
        return 1

    if last == "processed":
        return 0

    # Pipeline reported an error: record it for later requeue.
    if not args.no_dead_letter:
        record_dead_letter(args.video, "error",
                           extra={"processor_status": last_body})
    return 1


def cmd_dashboard(args):
    """Aggregate health + per-service /status + /metrics into one view."""
    report = collect_dashboard()
    if args.html:
        html = render_dashboard_html(report)
        out = Path(args.html)
        out.write_text(html, encoding="utf-8")
        print(f"Wrote dashboard to {out.resolve()}")
        return 0 if report["overall_ok"] else 1

    if args.json:
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["overall_ok"] else 1

    print("=" * 60)
    print(f"videoprocessor dashboard  ({report['generated_at']})")
    print("=" * 60)
    for svc in report["services"]:
        flag = "OK  " if svc["healthy"] else "DOWN"
        print(f"[{flag}] {svc['name']:<18} {svc['url']}")
        if svc.get("health_detail"):
            print(f"        health: {svc['health_detail']}")
        if svc.get("metrics"):
            for k, v in svc["metrics"].items():
                print(f"        metric {k} = {v}")
        if svc.get("status_summary"):
            print(f"        status: {svc['status_summary']}")
    dl = report["dead_letter_count"]
    print("-" * 60)
    print(f"dead-letter jobs: {dl}")
    healthy = sum(1 for s in report["services"] if s["healthy"])
    print(f"services healthy: {healthy}/{len(report['services'])}")
    return 0 if report["overall_ok"] else 1


def _parse_metrics_text(text):
    """Parse Prometheus text exposition into a flat {metric: value} dict."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        # ``name{labels} value`` -> split on last space.
        try:
            key_part, value = line.rsplit(" ", 1)
        except ValueError:
            continue
        name = key_part.split("{", 1)[0]
        out[name] = value
    return out


def collect_dashboard():
    services = []
    overall_ok = True
    for name, base in SERVICES.items():
        h_status, h_body = _http_get(f"{base}/health")
        healthy = h_status == 200 and h_body.get("status") == "healthy"
        overall_ok = overall_ok and healthy
        entry = {
            "name": name,
            "url": base,
            "healthy": healthy,
            "health_detail": h_body,
        }
        # Best-effort metrics + status (not all services expose /status).
        # Metrics are Prometheus text, so fetch the raw body (not JSON).
        text = _http_get_text(f"{base}/metrics")
        if text:
            entry["metrics"] = _parse_metrics_text(text)
        if name in STATUS_SERVICES:
            s_status, s_body = _http_get(f"{base}/status")
            if s_status == 200:
                entry["status_summary"] = _summarize_status(name, s_body)
        services.append(entry)
    return {
        "generated_at": _now_iso(),
        "services": services,
        "dead_letter_count": len(load_dead_letters()),
        "overall_ok": overall_ok,
    }


def _http_get_text(url, timeout=10):
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            return resp.read().decode("utf-8")
    except Exception:  # noqa: BLE001
        return None


def _summarize_status(name, body):
    if name == "video-scraper":
        return (f"detected={body.get('detected_count')} "
                f"moved={body.get('moved_count')} "
                f"errors={body.get('error_count')}")
    jobs = body.get("jobs", {})
    if isinstance(jobs, dict):
        counts = {}
        for job in jobs.values():
            st = job.get("status", "unknown") if isinstance(job, dict) else "unknown"
            counts[st] = counts.get(st, 0) + 1
        return ", ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no jobs"
    return str(jobs)


def render_dashboard_html(report):
    rows = []
    for svc in report["services"]:
        color = "#2e7d32" if svc["healthy"] else "#c62828"
        label = "HEALTHY" if svc["healthy"] else "DOWN"
        metrics_html = "<br>".join(
            f"<code>{k}={v}</code>" for k, v in (svc.get("metrics") or {}).items()
        ) or "&mdash;"
        status_html = svc.get("status_summary") or "&mdash;"
        rows.append(
            f"<tr>"
            f"<td>{svc['name']}</td>"
            f"<td><span style='color:{color};font-weight:bold'>{label}</span></td>"
            f"<td><a href='{svc['url']}'>{svc['url']}</a></td>"
            f"<td>{status_html}</td>"
            f"<td>{metrics_html}</td>"
            f"</tr>"
        )
    healthy = sum(1 for s in report["services"] if s["healthy"])
    return f"""<!doctype html>
<html lang="en"><head><meta charset="utf-8">
<title>videoprocessor dashboard</title>
<style>
 body {{ font-family: system-ui, sans-serif; margin: 2rem; color: #222; }}
 h1 {{ font-size: 1.4rem; }}
 table {{ border-collapse: collapse; width: 100%; margin-top: 1rem; }}
 th, td {{ border: 1px solid #ccc; padding: 8px 10px; text-align: left;
           vertical-align: top; font-size: 0.9rem; }}
 th {{ background: #f4f4f4; }}
 .meta {{ color: #666; font-size: 0.85rem; }}
</style></head>
<body>
<h1>videoprocessor dashboard</h1>
<p class="meta">Generated {report['generated_at']} &middot;
 services healthy: {healthy}/{len(report['services'])} &middot;
 dead-letter jobs: {report['dead_letter_count']}</p>
<table>
<thead><tr><th>Service</th><th>Health</th><th>URL</th>
<th>Status</th><th>Metrics</th></tr></thead>
<tbody>
{chr(10).join(rows)}
</tbody>
</table>
<p class="meta">Static snapshot &mdash; re-run
 <code>orchestrator.py dashboard --html</code> to refresh.</p>
</body></html>
"""


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
    rn.add_argument("--no-dead-letter", action="store_true",
                    help="Do not record failed/timed-out jobs to "
                         f"{DEAD_LETTER_DIR}/.")
    rn.set_defaults(func=cmd_run)

    db = sub.add_parser("dashboard",
                        help="Aggregated health + metrics for all services.")
    db.add_argument("--html", metavar="FILE",
                    help="Write a static HTML dashboard to FILE.")
    db.add_argument("--json", action="store_true",
                    help="Emit the raw aggregated report as JSON.")
    db.set_defaults(func=cmd_dashboard)

    dl = sub.add_parser("dead-letter",
                        help=f"List failed jobs in {DEAD_LETTER_DIR}/.")
    dl.set_defaults(func=cmd_dead_letter)

    rq = sub.add_parser("requeue",
                        help="Re-submit dead-lettered jobs into the pipeline.")
    rq.add_argument("--all", action="store_true",
                    help="Requeue every dead-letter record.")
    rq.add_argument("--name", help="Requeue a single job by name/stem.")
    rq.add_argument("--input-dir", default=INPUT_DIR)
    rq.set_defaults(func=cmd_requeue)

    return p


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
