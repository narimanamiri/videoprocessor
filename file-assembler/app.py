import json
import os
import shutil
from pathlib import Path

from flask import Flask, request, jsonify

from common import get_logger, post_with_retry, env_int

logger = get_logger("file-assembler")
app = Flask(__name__)

VIDEO_EXTENSIONS = {".mp4", ".mov", ".avi", ".mkv", ".wmv", ".flv", ".webm"}


def _safe_stem(video_name):
    """Strip a known video extension (suffix only) and sanitize for a folder.

    The previous implementation used ``str.replace('.mp4', '')`` which removed
    the substring anywhere in the name and only handled three extensions. This
    strips the real suffix and guards against path traversal.
    """
    name = (video_name or "unknown").strip()
    stem = Path(name).stem if Path(name).suffix.lower() in VIDEO_EXTENSIONS \
        else name
    # Prevent directory traversal / separators leaking into the folder name.
    stem = stem.replace("/", "_").replace("\\", "_").strip(". ")
    return stem or "unknown"


class FileAssembler:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)

    def assemble_final_output(self, video_data):
        try:
            video_name = _safe_stem(video_data.get("video_name"))
            final_folder = self.output_dir / video_name
            final_folder.mkdir(parents=True, exist_ok=True)

            results = {
                "video_name": video_name,
                "output_folder": str(final_folder),
                "files": [],
                "metadata": {},
                "status": "files_assembled",
            }

            # Copy processed video.
            if video_data.get("processed_path"):
                video_src = Path(video_data["processed_path"])
                if video_src.exists():
                    video_dest = final_folder / f"final_{video_src.name}"
                    shutil.copy2(video_src, video_dest)
                    results["files"].append({
                        "type": "video",
                        "path": str(video_dest),
                        "name": video_dest.name,
                    })
                    results["metadata"]["video_file"] = str(video_dest)
                else:
                    logger.warning("processed_path missing on disk: %s",
                                   video_src)

            # Copy thumbnail if the processor produced one.
            if video_data.get("thumbnail_path"):
                thumb_src = Path(video_data["thumbnail_path"])
                if thumb_src.exists():
                    thumb_dest = final_folder / thumb_src.name
                    shutil.copy2(thumb_src, thumb_dest)
                    results["files"].append({
                        "type": "thumbnail",
                        "path": str(thumb_dest),
                        "name": thumb_dest.name,
                    })

            # Copy subtitle / transcript files.
            for file_type in ("srt_path", "transcript_path"):
                if video_data.get(file_type):
                    file_src = Path(video_data[file_type])
                    if file_src.exists():
                        file_dest = final_folder / file_src.name
                        shutil.copy2(file_src, file_dest)
                        results["files"].append({
                            "type": "subtitle" if file_type == "srt_path"
                            else "transcript",
                            "path": str(file_dest),
                            "name": file_dest.name,
                        })

            # Save AI-generated captions as JSON + text.
            if video_data.get("title") and video_data.get("description"):
                caption_file = final_folder / "youtube_metadata.json"
                with open(caption_file, "w", encoding="utf-8") as f:
                    json.dump({
                        "title": video_data.get("title"),
                        "description": video_data.get("description"),
                        "tags": video_data.get("tags", []),
                        "video_type": video_data.get("video_type", "standard"),
                        "duration": video_data.get("duration", 0),
                    }, f, indent=2, ensure_ascii=False)
                results["files"].append({
                    "type": "metadata",
                    "path": str(caption_file),
                    "name": caption_file.name,
                })

                text_file = final_folder / "video_captions.txt"
                with open(text_file, "w", encoding="utf-8") as f:
                    f.write(f"Title: {video_data.get('title', '')}\n")
                    f.write(f"Video Type: {video_data.get('video_type', 'standard')}\n")
                    f.write(f"Duration: {video_data.get('duration', 0)} seconds\n\n")
                    f.write(f"Description:\n{video_data.get('description', '')}\n\n")
                    f.write(f"Tags: {', '.join(video_data.get('tags', []))}\n")
                results["files"].append({
                    "type": "captions",
                    "path": str(text_file),
                    "name": text_file.name,
                })

            # Always write a processing summary.
            summary_file = final_folder / "processing_summary.json"
            with open(summary_file, "w", encoding="utf-8") as f:
                json.dump({
                    "original_video": video_data.get("original_path"),
                    "video_type": video_data.get("video_type"),
                    "duration": video_data.get("duration"),
                    "processing_steps": "completed",
                    "transcript_length": len(video_data.get("transcript", "")),
                    "language": video_data.get("language", "en"),
                }, f, indent=2)
            results["files"].append({
                "type": "summary",
                "path": str(summary_file),
                "name": summary_file.name,
            })

            logger.info("Final output assembled in %s (%s files)",
                        final_folder, len(results["files"]))
            return results

        except Exception as exc:  # noqa: BLE001
            logger.exception("Error assembling final output")
            return {"error": str(exc), "status": "error"}

    def send_to_next_step(self, result):
        url = os.getenv("N8N_WEBHOOK_URL",
                        "http://n8n:5678/webhook/files-assembled")
        response = post_with_retry(url, result, logger=logger)
        return response.status_code if response is not None else None


assembler = FileAssembler(output_dir=os.getenv("OUTPUT_DIR", "/app/output"))


@app.route("/assemble-files", methods=["POST"])
def assemble_files_endpoint():
    try:
        data = request.get_json(silent=True) or {}
        logger.info("Assembling final files for: %s",
                    data.get("video_name", "unknown"))
        result = assembler.assemble_final_output(data)
        if "error" in result:
            return jsonify(result), 500
        assembler.send_to_next_step(result)
        return jsonify(result)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error in /assemble-files")
        return jsonify({"error": str(exc)}), 500


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({"status": "healthy", "service": "file-assembler"})


def main():
    port = env_int("PORT", 5004)
    logger.info("File Assembler started on port %s. Waiting for requests...",
                port)
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
