import os
import json
from pathlib import Path
from llama_cpp import Llama

class AICaptionAgent:
    def __init__(self, model_path):
        print("Loading AI model...")
        self.llm = Llama(
            model_path=model_path,
            n_ctx=2048,  # Context window
            n_threads=8,  # Adjust based on your CPU
            verbose=False
        )
        print("AI model loaded successfully")
    
    def generate_captions(self, transcript, video_type, video_name):
        """Generate engaging titles and descriptions using local LLM"""
        
        prompt = self._create_prompt(transcript, video_type, video_name)
        
        try:
            response = self.llm(
                prompt,
                max_tokens=512,
                temperature=0.7,
                top_p=0.9,
                stop=["###", "Human:"],
                echo=False
            )
            
            generated_text = response['choices'][0]['text'].strip()
            return self._parse_response(generated_text, video_name)
            
        except Exception as e:
            print(f"Error generating captions: {e}")
            return self._create_fallback_captions(video_name, video_type)
    
    def _create_prompt(self, transcript, video_type, video_name):
        return f"""Human: Generate engaging YouTube metadata for this {video_type} video.

Video Transcript: {transcript[:1000]}...

Please provide:
1. A catchy title (max 60 characters)
2. An engaging description (2-3 paragraphs)
3. 5 relevant tags

Format your response exactly as:
TITLE: [your title here]
DESCRIPTION: [your description here]
TAGS: [tag1, tag2, tag3, tag4, tag5]

Ensure the title is attention-grabbing and the description encourages viewers to watch the video.
"""

    def _parse_response(self, response_text, video_name):
        lines = response_text.split('\n')
        title = "Engaging Video Title"
        description = "Video description will appear here."
        tags = ["video", "content"]
        
        for line in lines:
            if line.startswith('TITLE:'):
                title = line.replace('TITLE:', '').strip()
            elif line.startswith('DESCRIPTION:'):
                description = line.replace('DESCRIPTION:', '').strip()
            elif line.startswith('TAGS:'):
                tags_str = line.replace('TAGS:', '').strip()
                tags = [tag.strip() for tag in tags_str.split(',')]
        
        # Fallback if parsing failed
        if not title or title == "Engaging Video Title":
            title = f"Awesome {video_name} Video"
        
        return {
            "title": title,
            "description": description,
            "tags": tags
        }
    
    def _create_fallback_captions(self, video_name, video_type):
        return {
            "title": f"Great {video_type} - {video_name}",
            "description": f"Watch this amazing {video_type} video about {video_name}. Don't forget to like and subscribe!",
            "tags": [video_type, video_name, "video", "content", "awesome"]
        }

def main():
    model_path = os.getenv('MODEL_PATH', '/app/models/llama-2-7b-chat.q4_0.gguf')
    
    if not Path(model_path).exists():
        print(f"Warning: Model not found at {model_path}")
        print("Please download a GGUF model and place it in the models directory")
    
    agent = AICaptionAgent(model_path)
    print("AI Caption Agent started. Waiting for n8n triggers...")

if __name__ == "__main__":
    main()