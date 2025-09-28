import os
import json
import whisper
import requests
from pathlib import Path
from flask import Flask, request, jsonify
import ffmpeg

app = Flask(__name__)

class CaptionGenerator:
    def __init__(self):
        print("Loading Whisper model...")
        # Load Whisper model (base model for balance of speed/accuracy)
        self.model = whisper.load_model("base")
        print("Whisper model loaded successfully")
    
    def extract_audio(self, video_path, audio_path):
        """Extract audio from video for transcription"""
        try:
            print(f"Extracting audio from {video_path}")
            (
                ffmpeg
                .input(str(video_path))
                .output(str(audio_path), format='wav', ac=1, ar='16000')
                .overwrite_output()
                .run(quiet=True)
            )
            print(f"Audio extracted to {audio_path}")
        except Exception as e:
            print(f"Error extracting audio: {e}")
            raise
    
    def generate_subtitles(self, video_path, output_dir):
        """Generate SRT subtitles from video"""
        try:
            video_path = Path(video_path)
            output_dir = Path(output_dir)
            
            if not video_path.exists():
                raise FileNotFoundError(f"Video file not found: {video_path}")
            
            # Create output directory if it doesn't exist
            output_dir.mkdir(parents=True, exist_ok=True)
            
            # Extract audio temporarily
            audio_path = output_dir / "temp_audio.wav"
            self.extract_audio(video_path, audio_path)
            
            print("Transcribing audio...")
            # Transcribe audio
            result = self.model.transcribe(str(audio_path))
            
            # Generate SRT file
            srt_path = output_dir / f"{video_path.stem}.srt"
            self.create_srt_file(result['segments'], srt_path)
            
            # Generate transcript text
            transcript_path = output_dir / f"{video_path.stem}_transcript.txt"
            self.create_transcript_file(result['text'], transcript_path)
            
            # Clean up temporary audio file
            audio_path.unlink(missing_ok=True)
            
            print(f"Subtitles generated: {srt_path}")
            print(f"Transcript generated: {transcript_path}")
            
            return {
                "srt_path": str(srt_path),
                "transcript_path": str(transcript_path),
                "transcript": result['text'],
                "language": result.get('language', 'en'),
                "status": "subtitles_generated"
            }
            
        except Exception as e:
            print(f"Error generating subtitles: {e}")
            return {"error": str(e), "status": "error"}
    
    def create_srt_file(self, segments, srt_path):
        """Create SRT format subtitle file"""
        with open(srt_path, 'w', encoding='utf-8') as f:
            for i, segment in enumerate(segments, 1):
                start = self.format_timestamp(segment['start'])
                end = self.format_timestamp(segment['end'])
                text = segment['text'].strip()
                
                f.write(f"{i}\n")
                f.write(f"{start} --> {end}\n")
                f.write(f"{text}\n\n")
    
    def create_transcript_file(self, transcript, transcript_path):
        """Create plain text transcript file"""
        with open(transcript_path, 'w', encoding='utf-8') as f:
            f.write(transcript)
    
    def format_timestamp(self, seconds):
        """Convert seconds to SRT timestamp format"""
        hours = int(seconds // 3600)
        minutes = int((seconds % 3600) // 60)
        secs = seconds % 60
        millis = int((secs - int(secs)) * 1000)
        secs = int(secs)
        return f"{hours:02d}:{minutes:02d}:{secs:02d},{millis:03d}"

    def send_to_next_step(self, result):
        try:
            n8n_webhook_url = os.getenv('N8N_WEBHOOK_URL', 'http://n8n:5678/webhook/subtitles-generated')
            response = requests.post(
                n8n_webhook_url,
                json=result,
                timeout=30
            )
            print(f"Sent subtitles to n8n: {response.status_code}")
            return response.status_code
        except Exception as e:
            print(f"Failed to send to n8n: {e}")
            return None

# Initialize caption generator
generator = CaptionGenerator()

@app.route('/generate-subtitles', methods=['POST'])
def generate_subtitles_endpoint():
    try:
        data = request.json
        video_path = data.get('video_path')
        processed_path = data.get('processed_path')
        
        if not video_path and not processed_path:
            return jsonify({"error": "No video path provided"}), 400
        
        # Use processed video if available, otherwise use original
        target_video_path = processed_path if processed_path else video_path
        
        output_dir = os.getenv('OUTPUT_DIR', '/app/output')
        
        print(f"Generating subtitles for: {target_video_path}")
        result = generator.generate_subtitles(target_video_path, output_dir)
        
        if 'error' in result:
            return jsonify(result), 500
        
        # Merge with incoming data
        result.update(data)
        
        # Send to next step
        generator.send_to_next_step(result)
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "service": "caption-generator"})

def main():
    port = int(os.getenv('PORT', '5002'))
    print(f"Caption Generator started on port {port}. Waiting for requests...")
    app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == "__main__":
    main()