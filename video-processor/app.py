import os
import json
import requests
from pathlib import Path
import ffmpeg

class VideoProcessor:
    def __init__(self, processing_dir, output_dir, n8n_webhook_url):
        self.processing_dir = Path(processing_dir)
        self.output_dir = Path(output_dir)
        self.n8n_webhook_url = n8n_webhook_url
        
    def get_video_duration(self, video_path):
        try:
            probe = ffmpeg.probe(str(video_path))
            duration = float(probe['streams'][0]['duration'])
            return duration
        except Exception as e:
            print(f"Error getting video duration: {e}")
            return None
    
    def process_video(self, video_path):
        try:
            video_path = Path(video_path)
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
                "output_filename": output_filename
            }
            
            # Send to next step in n8n workflow
            self.send_to_next_step(result)
            
            return result
            
        except Exception as e:
            print(f"Error processing video: {e}")
            return None
    
    def process_shorts(self, input_path, output_path):
        # Process for YouTube Shorts - vertical 9:16
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
                timeout=10
            )
            print(f"Sent processed video to n8n: {response.status_code}")
        except Exception as e:
            print(f"Failed to send to n8n: {e}")

def main():
    processing_dir = os.getenv('PROCESSING_DIR', '/app/processing')
    output_dir = os.getenv('OUTPUT_DIR', '/app/output')
    n8n_webhook_url = os.getenv('N8N_WEBHOOK_URL', 'http://n8n:5678/webhook/video-processed')
    
    processor = VideoProcessor(processing_dir, output_dir, n8n_webhook_url)
    
    print("Video Processor started. Waiting for n8n triggers...")
    
    # This service will be triggered by n8n webhook

if __name__ == "__main__":
    main()