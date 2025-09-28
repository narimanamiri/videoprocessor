import os
import json
import whisper
from pathlib import Path

class CaptionGenerator:
    def __init__(self):
        # Load Whisper model (base model for balance of speed/accuracy)
        self.model = whisper.load_model("base")
    
    def extract_audio(self, video_path, audio_path):
        """Extract audio from video for transcription"""
        import ffmpeg
        (
            ffmpeg
            .input(str(video_path))
            .output(str(audio_path), format='wav', ac=1, ar='16000')
            .overwrite_output()
            .run(quiet=True)
        )
    
    def generate_subtitles(self, video_path, output_dir):
        """Generate SRT subtitles from video"""
        try:
            video_path = Path(video_path)
            output_dir = Path(output_dir)
            
            # Extract audio temporarily
            audio_path = output_dir / "temp_audio.wav"
            self.extract_audio(video_path, audio_path)
            
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
            
            return {
                "srt_path": str(srt_path),
                "transcript_path": str(transcript_path),
                "transcript": result['text']
            }
            
        except Exception as e:
            print(f"Error generating subtitles: {e}")
            return None
    
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
        return f"{hours:02d}:{minutes:02d}:{secs:06.3f}".replace('.', ',')

def main():
    output_dir = os.getenv('OUTPUT_DIR', '/app/output')
    Path(output_dir).mkdir(parents=True, exist_ok=True)
    
    generator = CaptionGenerator()
    print("Caption Generator started. Waiting for n8n triggers...")

if __name__ == "__main__":
    main()