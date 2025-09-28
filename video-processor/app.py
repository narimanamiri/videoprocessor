import os
import json
import requests
import time
from pathlib import Path
import ffmpeg
from flask import Flask, request, jsonify

app = Flask(__name__)

class VideoProcessor:
    def __init__(self, processing_dir, output_dir, n8n_webhook_url):
        self.processing_dir = Path(processing_dir)
        self.output_dir = Path(output_dir)
        self.n8n_webhook_url = n8n_webhook_url
        
        # Create directories
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
    def get_video_duration(self, video_path):
        try:
            probe = ffmpeg.probe(str(video_path))
            video_stream = next((stream for stream in probe['streams'] if stream['codec_type'] == 'video'), None)
            if video_stream:
                duration = float(video_stream.get('duration', 0))
                return duration
            return None
        except Exception as e:
            print(f"Error getting video duration: {e}")
            return None
    
    def process_video(self, video_path):
        try:
            video_path = Path(video_path)
            if not video_path.exists():
                raise FileNotFoundError(f"Video file not found: {video_path}")
                
            duration = self.get_video_duration(video_path)
            
            if duration is None:
                raise Exception("Could not determine video duration")
            
            # Determine video type based on duration
            is_short = duration <= 60  # 60 seconds for Shorts
            
            output_filename = f"processed_{video_path.stem}.mp4"
            output_path = self.output_dir / output_filename
            
            if is_short:
                # Process for YouTube Shorts (9:16 aspect ratio, vertical)
                self.process_shorts(video_path, output_path)
                video_type = "shorts"
            else:
                # Process for regular YouTube (16:9 aspect ratio)
                self.process_standard(video_path, output_path)
                video_type = "standard"
            
            result = {
                "original_path": str(video_path),
                "processed_path": str(output_path),
                "duration": duration,
                "video_type": video_type,
                "output_filename": output_filename,
                "status": "processed"
            }
            
            return result
            
        except Exception as e:
            print(f"Error processing video: {e}")
            return {"error": str(e), "status": "error"}
    
    def process_shorts(self, input_path, output_path):
        # Process for YouTube Shorts - vertical 9:16
        print(f"Processing as Shorts: {input_path} -> {output_path}")
        (
            ffmpeg
            .input(str(input_path))
            .filter('scale', 1080, 1920)  # Vertical format
            .filter('pad', 1080, 1920, -1, -1, 'black')  # Add black bars if needed
            .output(str(output_path), 
                   vcodec='libx264', 
                   acodec='aac',
                   **{'b:v': '2M', 'b:a': '192k'})
            .overwrite_output()
            .run(quiet=True)
        )
    
    def process_standard(self, input_path, output_path):
        # Process for standard YouTube - horizontal 16:9
        print(f"Processing as Standard: {input_path} -> {output_path}")
        (
            ffmpeg
            .input(str(input_path))
            .filter('scale', 1920, 1080)  # Standard HD
            .output(str(output_path), 
                   vcodec='libx264', 
                   acodec='aac',
                   **{'b:v': '5M', 'b:a': '192k'})
            .overwrite_output()
            .run(quiet=True)
        )
    
    def send_to_next_step(self, result):
        try:
            response = requests.post(
                self.n8n_webhook_url,
                json=result,
                timeout=30
            )
            print(f"Sent processed video to n8n: {response.status_code}")
            return response.status_code
        except Exception as e:
            print(f"Failed to send to n8n: {e}")
            return None

# Initialize processor
processor = VideoProcessor(
    processing_dir=os.getenv('PROCESSING_DIR', '/app/processing'),
    output_dir=os.getenv('OUTPUT_DIR', '/app/output'),
    n8n_webhook_url=os.getenv('N8N_WEBHOOK_URL', 'http://n8n:5678/webhook/video-processed')
)

@app.route('/process', methods=['POST'])
def process_video_endpoint():
    try:
        data = request.json
        video_path = data.get('video_path')
        
        if not video_path:
            return jsonify({"error": "No video_path provided"}), 400
        
        print(f"Processing video: {video_path}")
        result = processor.process_video(video_path)
        
        if 'error' in result:
            return jsonify(result), 500
        
        # Send to next step
        processor.send_to_next_step(result)
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "service": "video-processor"})

def main():
    port = int(os.getenv('PORT', '5000'))
    print(f"Video Processor started on port {port}. Waiting for requests...")
    app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == "__main__":
    main()