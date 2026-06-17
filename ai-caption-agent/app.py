import os
import time

import requests
from flask import Flask, request, jsonify

from common import get_logger, post_with_retry, env_int, env_float

logger = get_logger("ai-caption-agent")
app = Flask(__name__)


class AICaptionAgent:
    def __init__(self):
        # FEATURE: Ollama endpoint, model and tuning are env-configurable.
        host = os.getenv("OLLAMA_API_HOST", "http://localhost:11434")
        self.ollama_base = host.rstrip("/")
        self.ollama_url = f"{self.ollama_base}/api/generate"
        self.tags_url = f"{self.ollama_base}/api/tags"
        self.model_name = os.getenv("OLLAMA_MODEL", "llama3.1:8b")
        self.temperature = env_float("OLLAMA_TEMPERATURE", 0.7)
        self.gen_timeout = env_float("OLLAMA_TIMEOUT", 120.0)
        self.model_loaded = False
        self.ensure_model_loaded()

    def ensure_model_loaded(self):
        """Wait (with backoff) for Ollama to be reachable and the model present."""
        max_retries = env_int("OLLAMA_READY_RETRIES", 5)
        wait = env_float("OLLAMA_READY_WAIT", 5.0)
        model_family = self.model_name.split(":")[0].lower()

        for i in range(max_retries):
            try:
                response = requests.get(self.tags_url, timeout=10)
                if response.status_code == 200:
                    models = response.json().get("models", [])
                    if any(model_family in m.get("name", "").lower()
                           for m in models):
                        self.model_loaded = True
                        logger.info("Ollama model '%s' is ready", self.model_name)
                        return
                    logger.info("Model '%s' not found in Ollama yet, waiting...",
                                self.model_name)
                else:
                    logger.warning("Ollama not ready, status: %s",
                                   response.status_code)
            except requests.RequestException:
                logger.info("Waiting for Ollama to start... (%s/%s)",
                            i + 1, max_retries)
            time.sleep(wait)

        logger.warning("Ollama model may not be ready after %s attempts",
                       max_retries)

    def generate_with_ollama(self, prompt):
        try:
            payload = {
                "model": self.model_name,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": self.temperature,
                    "top_p": env_float("OLLAMA_TOP_P", 0.9),
                    "top_k": env_int("OLLAMA_TOP_K", 40),
                },
            }
            response = requests.post(self.ollama_url, json=payload,
                                     timeout=self.gen_timeout)
            response.raise_for_status()
            return response.json().get("response", "").strip()
        except requests.RequestException as exc:
            logger.error("Error calling Ollama API: %s", exc)
            return ""

    def generate_captions(self, transcript, video_type, video_name, duration):
        prompt = self._create_prompt(transcript, video_type, video_name, duration)
        logger.info("Generating AI captions for %s", video_name)
        generated_text = self.generate_with_ollama(prompt)
        if not generated_text:
            logger.warning("Empty AI response; using fallback captions")
            return self._create_fallback_captions(video_name, video_type)
        return self._parse_response(generated_text, video_name, video_type)

    def _create_prompt(self, transcript, video_type, video_name, duration):
        transcript_preview = (
            transcript[:800] + "..." if len(transcript) > 800 else transcript
        )
        return f"""Create engaging YouTube metadata for a {video_type} video.

VIDEO DETAILS:
- Type: {video_type}
- Duration: {duration} seconds
- Name: {video_name}

TRANSCRIPT:
{transcript_preview}

Please generate:
1. A catchy, attention-grabbing title (under 60 characters)
2. A compelling description (2-3 paragraphs that hook viewers)
3. 5-7 relevant tags

Format your response EXACTLY like this:
TITLE: [Your catchy title here]
DESCRIPTION: [Your engaging description here.]
TAGS: [tag1, tag2, tag3, tag4, tag5, tag6]

Make the title viral-worthy and the description persuasive."""

    def _parse_response(self, response_text, video_name, video_type):
        """Parse the AI response into structured data.

        Handles multi-line descriptions correctly: everything after the
        ``DESCRIPTION:`` marker (until ``TAGS:``) is treated as the body.
        """
        title = f"Awesome {video_type} - {video_name}"
        description = (
            f"Watch this engaging {video_type} video about {video_name}. "
            "Don't forget to like and subscribe for more content!"
        )
        tags = [video_type, video_name, "video", "content", "awesome"]

        lines = response_text.split("\n")
        desc_lines = []
        capturing_desc = False

        for line in lines:
            stripped = line.strip()
            upper = stripped.upper()
            if upper.startswith("TITLE:"):
                capturing_desc = False
                title = stripped[len("TITLE:"):].strip()
                if len(title) > 60:
                    title = title[:57] + "..."
            elif upper.startswith("DESCRIPTION:"):
                capturing_desc = True
                first = stripped[len("DESCRIPTION:"):].strip()
                if first:
                    desc_lines.append(first)
            elif upper.startswith("TAGS:"):
                capturing_desc = False
                tags_str = stripped[len("TAGS:"):].strip().strip("[]")
                parsed = [t.strip() for t in tags_str.split(",") if t.strip()]
                if parsed:
                    tags = parsed
            elif capturing_desc:
                desc_lines.append(line.rstrip())

        if desc_lines:
            description = "\n".join(desc_lines).strip()

        return {
            "title": title,
            "description": description,
            "tags": tags,
            "status": "captions_generated",
        }

    def _create_fallback_captions(self, video_name, video_type):
        return {
            "title": f"Great {video_type} - {video_name}",
            "description": (
                f"Watch this amazing {video_type} video about {video_name}. "
                "Don't forget to like and subscribe for more great content!"
            ),
            "tags": [video_type, video_name, "video", "content", "must watch"],
            "status": "captions_generated",
        }

    def send_to_next_step(self, result):
        url = os.getenv("N8N_WEBHOOK_URL",
                        "http://n8n:5678/webhook/captions-generated")
        response = post_with_retry(url, result, logger=logger)
        return response.status_code if response is not None else None


ai_agent = AICaptionAgent()


@app.route("/generate-captions", methods=["POST"])
def generate_captions_endpoint():
    try:
        data = request.get_json(silent=True) or {}
        transcript = data.get("transcript", "")
        video_type = data.get("video_type", "standard")
        video_name = data.get("video_name", "Unknown")
        duration = data.get("duration", 0)

        if not transcript:
            return jsonify({"error": "No transcript provided"}), 400

        captions = ai_agent.generate_captions(
            transcript, video_type, video_name, duration
        )
        result = {**data, **captions}
        ai_agent.send_to_next_step(result)
        return jsonify(result)

    except Exception as exc:  # noqa: BLE001
        logger.exception("Unhandled error in /generate-captions")
        return jsonify({"error": str(exc)}), 500


@app.route("/health", methods=["GET"])
def health_check():
    return jsonify({
        "status": "healthy",
        "service": "ai-caption-agent",
        "model": ai_agent.model_name,
        "model_loaded": ai_agent.model_loaded,
    })


def main():
    port = env_int("PORT", 5003)
    logger.info("AI Caption Agent started on port %s. Waiting for requests...",
                port)
    app.run(host="0.0.0.0", port=port, debug=False)


if __name__ == "__main__":
    main()
