import os
import json
import requests
import time
from pathlib import Path
from flask import Flask, request, jsonify

app = Flask(__name__)

class AICaptionAgent:
    def __init__(self):
        self.ollama_url = "http://localhost:11434/api/generate"
        self.model_loaded = False
        self.ensure_model_loaded()
    
    def ensure_model_loaded(self):
        """Ensure the Ollama model is loaded and ready"""
        max_retries = 5
        for i in range(max_retries):
            try:
                # Check if Ollama is responding
                response = requests.get("http://localhost:11434/api/tags", timeout=10)
                if response.status_code == 200:
                    models = response.json().get('models', [])
                    if any('llama' in model.get('name', '').lower() for model in models):
                        self.model_loaded = True
                        print("Ollama model is ready")
                        return
                    else:
                        print("Llama model not found in Ollama, waiting...")
                else:
                    print(f"Ollama not ready, status: {response.status_code}")
            except Exception as e:
                print(f"Waiting for Ollama to start... ({i+1}/{max_retries})")
            
            time.sleep(5)
        
        print("Warning: Ollama model may not be ready")
    
    def generate_with_ollama(self, prompt, model_name="llama3.1:8b"):
        """Send prompt to Ollama and get response"""
        try:
            payload = {
                "model": model_name,
                "prompt": prompt,
                "stream": False,
                "options": {
                    "temperature": 0.7,
                    "top_p": 0.9,
                    "top_k": 40
                }
            }
            
            response = requests.post(self.ollama_url, json=payload, timeout=120)
            response.raise_for_status()
            result = response.json()
            return result.get('response', '').strip()
            
        except Exception as e:
            print(f"Error calling Ollama API: {e}")
            return ""
    
    def generate_captions(self, transcript, video_type, video_name, duration):
        """Generate engaging titles and descriptions using Ollama"""
        
        prompt = self._create_prompt(transcript, video_type, video_name, duration)
        
        print("Generating AI captions...")
        generated_text = self.generate_with_ollama(prompt)
        
        if not generated_text:
            return self._create_fallback_captions(video_name, video_type)
        
        return self._parse_response(generated_text, video_name, video_type)

    def _create_prompt(self, transcript, video_type, video_name, duration):
        transcript_preview = transcript[:800] + "..." if len(transcript) > 800 else transcript
        
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
DESCRIPTION: [Your engaging description here. Make it compelling and encourage viewers to watch. Include relevant details from the transcript.]
TAGS: [tag1, tag2, tag3, tag4, tag5, tag6]

Make the title viral-worthy and the description persuasive."""
    
    def _parse_response(self, response_text, video_name, video_type):
        """Parse the AI response into structured data"""
        lines = response_text.split('\n')
        title = f"Awesome {video_type} - {video_name}"
        description = f"Watch this engaging {video_type} video about {video_name}. Don't forget to like and subscribe for more content!"
        tags = [video_type, video_name, "video", "content", "awesome"]
        
        for line in lines:
            line = line.strip()
            if line.startswith('TITLE:'):
                title = line.replace('TITLE:', '').strip()
                # Ensure title is not too long
                if len(title) > 60:
                    title = title[:57] + "..."
            elif line.startswith('DESCRIPTION:'):
                description = line.replace('DESCRIPTION:', '').strip()
            elif line.startswith('TAGS:'):
                tags_str = line.replace('TAGS:', '').strip().strip('[]')
                tags = [tag.strip() for tag in tags_str.split(',') if tag.strip()]
        
        return {
            "title": title,
            "description": description,
            "tags": tags,
            "status": "captions_generated"
        }
    
    def _create_fallback_captions(self, video_name, video_type):
        """Create fallback captions if AI generation fails"""
        return {
            "title": f"Great {video_type} - {video_name}",
            "description": f"Watch this amazing {video_type} video about {video_name}. Don't forget to like and subscribe for more great content!",
            "tags": [video_type, video_name, "video", "content", "must watch"],
            "status": "captions_generated"
        }

    def send_to_next_step(self, result):
        try:
            n8n_webhook_url = os.getenv('N8N_WEBHOOK_URL', 'http://n8n:5678/webhook/captions-generated')
            response = requests.post(
                n8n_webhook_url,
                json=result,
                timeout=30
            )
            print(f"Sent AI captions to n8n: {response.status_code}")
            return response.status_code
        except Exception as e:
            print(f"Failed to send to n8n: {e}")
            return None

# Initialize AI agent
ai_agent = AICaptionAgent()

@app.route('/generate-captions', methods=['POST'])
def generate_captions_endpoint():
    try:
        data = request.json
        transcript = data.get('transcript', '')
        video_type = data.get('video_type', 'standard')
        video_name = data.get('video_name', 'Unknown')
        duration = data.get('duration', 0)
        
        if not transcript:
            return jsonify({"error": "No transcript provided"}), 400
        
        print(f"Generating AI captions for: {video_name}")
        captions = ai_agent.generate_captions(transcript, video_type, video_name, duration)
        
        # Merge with incoming data
        result = {**data, **captions}
        
        # Send to next step
        ai_agent.send_to_next_step(result)
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({
        "status": "healthy", 
        "service": "ai-caption-agent",
        "model_loaded": ai_agent.model_loaded
    })

def main():
    port = int(os.getenv('PORT', '5003'))
    print(f"AI Caption Agent started on port {port}. Waiting for requests...")
    app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == "__main__":
    main()