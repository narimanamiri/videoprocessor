import os
import time
import json
import requests
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler
from pathlib import Path

class VideoHandler(FileSystemEventHandler):
    def __init__(self, processing_dir, n8n_webhook_url):
        self.processing_dir = processing_dir
        self.n8n_webhook_url = n8n_webhook_url
        self.supported_formats = {'.mp4', '.avi', '.mov', '.mkv', '.wmv', '.flv', '.webm'}

    def on_created(self, event):
        if event.is_directory:
            return
        
        file_path = Path(event.src_path)
        if file_path.suffix.lower() in self.supported_formats:
            print(f"New video detected: {file_path}")
            self.process_video(file_path)

    def process_video(self, video_path):
        # Move to processing directory
        processing_path = Path(self.processing_dir) / video_path.name
        try:
            video_path.rename(processing_path)
            print(f"Moved video to processing: {processing_path}")
            
            # Send to n8n workflow
            payload = {
                "video_path": str(processing_path),
                "video_name": processing_path.name,
                "timestamp": time.time()
            }
            
            try:
                response = requests.post(
                    self.n8n_webhook_url,
                    json=payload,
                    timeout=10
                )
                print(f"Sent to n8n workflow: {response.status_code}")
            except Exception as e:
                print(f"Failed to send to n8n: {e}")
                
        except Exception as e:
            print(f"Error processing video {video_path}: {e}")

def main():
    input_dir = os.getenv('INPUT_DIR', '/app/input')
    processing_dir = os.getenv('PROCESSING_DIR', '/app/processing')
    n8n_webhook_url = os.getenv('N8N_WEBHOOK_URL', 'http://n8n:5678/webhook/video-detected')
    scan_interval = int(os.getenv('SCAN_INTERVAL', '30'))
    
    # Create directories if they don't exist
    Path(input_dir).mkdir(parents=True, exist_ok=True)
    Path(processing_dir).mkdir(parents=True, exist_ok=True)
    
    event_handler = VideoHandler(processing_dir, n8n_webhook_url)
    observer = Observer()
    observer.schedule(event_handler, input_dir, recursive=False)
    
    print(f"Starting video scraper. Watching: {input_dir}")
    observer.start()
    
    try:
        while True:
            time.sleep(scan_interval)
    except KeyboardInterrupt:
        observer.stop()
    observer.join()

if __name__ == "__main__":
    main()