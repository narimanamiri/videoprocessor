import os
import json
import shutil
import requests
from pathlib import Path
from flask import Flask, request, jsonify

app = Flask(__name__)

class FileAssembler:
    def __init__(self, output_dir):
        self.output_dir = Path(output_dir)
        
    def assemble_final_output(self, video_data):
        """Assemble all generated files into final output"""
        try:
            video_name = video_data.get('video_name', 'unknown').replace('.mp4', '').replace('.mov', '').replace('.avi', '')
            final_folder = self.output_dir / video_name
            
            # Create final output folder
            final_folder.mkdir(parents=True, exist_ok=True)
            
            # Copy/move all relevant files
            results = {
                "video_name": video_name,
                "output_folder": str(final_folder),
                "files": [],
                "metadata": {},
                "status": "files_assembled"
            }
            
            # Copy processed video
            if 'processed_path' in video_data:
                video_src = Path(video_data['processed_path'])
                if video_src.exists():
                    video_dest = final_folder / f"final_{video_src.name}"
                    shutil.copy2(video_src, video_dest)
                    results['files'].append({
                        "type": "video",
                        "path": str(video_dest),
                        "name": video_dest.name
                    })
                    results['metadata']['video_file'] = str(video_dest)
            
            # Copy subtitle files
            for file_type in ['srt_path', 'transcript_path']:
                if file_type in video_data:
                    file_src = Path(video_data[file_type])
                    if file_src.exists():
                        file_dest = final_folder / file_src.name
                        shutil.copy2(file_src, file_dest)
                        results['files'].append({
                            "type": "subtitle" if file_type == 'srt_path' else "transcript",
                            "path": str(file_dest),
                            "name": file_dest.name
                        })
            
            # Save AI generated captions
            if 'title' in video_data and 'description' in video_data:
                # Save as JSON
                caption_file = final_folder / "youtube_metadata.json"
                with open(caption_file, 'w', encoding='utf-8') as f:
                    json.dump({
                        "title": video_data.get('title'),
                        "description": video_data.get('description'),
                        "tags": video_data.get('tags', []),
                        "video_type": video_data.get('video_type', 'standard'),
                        "duration": video_data.get('duration', 0)
                    }, f, indent=2, ensure_ascii=False)
                results['files'].append({
                    "type": "metadata",
                    "path": str(caption_file),
                    "name": caption_file.name
                })
                
                # Also save as text file
                text_file = final_folder / "video_captions.txt"
                with open(text_file, 'w', encoding='utf-8') as f:
                    f.write(f"Title: {video_data.get('title', '')}\n")
                    f.write(f"Video Type: {video_data.get('video_type', 'standard')}\n")
                    f.write(f"Duration: {video_data.get('duration', 0)} seconds\n\n")
                    f.write(f"Description:\n{video_data.get('description', '')}\n\n")
                    f.write(f"Tags: {', '.join(video_data.get('tags', []))}\n")
                results['files'].append({
                    "type": "captions",
                    "path": str(text_file),
                    "name": text_file.name
                })
            
            # Save processing summary
            summary_file = final_folder / "processing_summary.json"
            with open(summary_file, 'w', encoding='utf-8') as f:
                json.dump({
                    "original_video": video_data.get('original_path'),
                    "video_type": video_data.get('video_type'),
                    "duration": video_data.get('duration'),
                    "processing_steps": "completed",
                    "transcript_length": len(video_data.get('transcript', '')),
                    "language": video_data.get('language', 'en')
                }, f, indent=2)
            results['files'].append({
                "type": "summary",
                "path": str(summary_file),
                "name": summary_file.name
            })
            
            print(f"Final output assembled in: {final_folder}")
            print(f"Generated {len(results['files'])} files")
            
            return results
            
        except Exception as e:
            print(f"Error assembling final output: {e}")
            return {"error": str(e), "status": "error"}

    def send_to_next_step(self, result):
        try:
            n8n_webhook_url = os.getenv('N8N_WEBHOOK_URL', 'http://n8n:5678/webhook/files-assembled')
            response = requests.post(
                n8n_webhook_url,
                json=result,
                timeout=30
            )
            print(f"Sent final assembly result to n8n: {response.status_code}")
            return response.status_code
        except Exception as e:
            print(f"Failed to send to n8n: {e}")
            return None

# Initialize file assembler
assembler = FileAssembler(
    output_dir=os.getenv('OUTPUT_DIR', '/app/output')
)

@app.route('/assemble-files', methods=['POST'])
def assemble_files_endpoint():
    try:
        data = request.json
        
        print("Assembling final files...")
        result = assembler.assemble_final_output(data)
        
        if 'error' in result:
            return jsonify(result), 500
        
        # Send to next step (completion)
        assembler.send_to_next_step(result)
        
        return jsonify(result)
        
    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health_check():
    return jsonify({"status": "healthy", "service": "file-assembler"})

def main():
    port = int(os.getenv('PORT', '5004'))
    print(f"File Assembler started on port {port}. Waiting for requests...")
    app.run(host='0.0.0.0', port=port, debug=False)

if __name__ == "__main__":
    main()