#!/usr/bin/env python3
"""
Interview Transcription with Speaker Diarization
=================================================
Combines OpenAI Whisper (transcription) + pyannote-audio (speaker diarization)
to produce labeled interview transcripts.

Usage:
    python transcribe_interview.py interview.mp3
    python transcribe_interview.py interview.mp3 --model large-v3 --num-speakers 2
    python transcribe_interview.py ./interviews/  # process entire folder

Requirements:
    pip install openai-whisper pyannote.audio torch torchaudio pydub

Setup:
    1. Create a Hugging Face account at https://huggingface.co
    2. Accept the pyannote model licenses:
       - https://huggingface.co/pyannote/segmentation-3.0
       - https://huggingface.co/pyannote/speaker-diarization-3.1
    3. Create an access token at https://huggingface.co/settings/tokens
    4. Set your token:
       export HF_TOKEN="hf_your_token_here"
"""

import argparse
import os
import sys
import json
import time
from pathlib import Path
from datetime import timedelta

import subprocess
import shutil
from dotenv import load_dotenv

import whisper
import torch
from pyannote.audio import Pipeline as DiarizationPipeline


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

SUPPORTED_EXTENSIONS = {".mp3", ".wav", ".m4a", ".flac", ".ogg", ".wma", ".aac", ".webm", ".mp4"}


def format_timestamp(seconds: float) -> str:
    """Convert seconds to HH:MM:SS format."""
    td = timedelta(seconds=seconds)
    total_seconds = int(td.total_seconds())
    hours, remainder = divmod(total_seconds, 3600)
    minutes, secs = divmod(remainder, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def convert_to_wav(input_path: str, output_path: str) -> str:
    """Convert any supported audio format to 16kHz mono WAV using ffmpeg."""
    print(f"  Converting to WAV (16kHz mono)...")
    if not shutil.which("ffmpeg"):
        print("ERROR: ffmpeg not found. Install with: sudo apt install ffmpeg")
        sys.exit(1)
    result = subprocess.run(
        ["ffmpeg", "-y", "-i", input_path, "-ar", "16000", "-ac", "1", "-c:a", "pcm_s16le", output_path],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        print(f"ERROR: ffmpeg conversion failed:\n{result.stderr}")
        sys.exit(1)
    return output_path


def get_device() -> str:
    """Detect best available device."""
    if torch.cuda.is_available():
        print(f"  Using GPU: {torch.cuda.get_device_name(0)}")
        return "cuda"
    else:
        print("  Using CPU (GPU not detected — this will be slower)")
        return "cpu"


# ---------------------------------------------------------------------------
# Transcription (Whisper)
# ---------------------------------------------------------------------------

def transcribe_audio(audio_path: str, model_name: str, device: str) -> dict:
    """Transcribe audio using Whisper and return word-level segments."""
    print(f"  Loading Whisper model '{model_name}'...")
    model = whisper.load_model(model_name, device=device)

    print(f"  Transcribing (this may take a while for large files)...")
    result = model.transcribe(
        audio_path,
        verbose=False,
        word_timestamps=True,
        language=None,  # auto-detect language
    )

    print(f"  Detected language: {result.get('language', 'unknown')}")
    return result


# ---------------------------------------------------------------------------
# Diarization (pyannote)
# ---------------------------------------------------------------------------

def diarize_audio(audio_path: str, hf_token: str, num_speakers: int = None, device: str = "cpu") -> list:
    """Run speaker diarization and return list of (start, end, speaker) tuples."""
    print(f"  Loading diarization pipeline...")
    pipeline = DiarizationPipeline.from_pretrained(
        "pyannote/speaker-diarization-3.1",
        token=hf_token,
    )

    if device == "cuda":
        pipeline.to(torch.device("cuda"))

    print(f"  Running speaker diarization...")
    diarization_args = {}
    if num_speakers is not None:
        diarization_args["num_speakers"] = num_speakers
        print(f"  (fixed to {num_speakers} speakers)")

    diarization = pipeline(audio_path, **diarization_args)

    segments = []

    # pyannote 3.x returns an Annotation with itertracks()
    # pyannote 4.x returns a DiarizeOutput with speaker_diarization
    if hasattr(diarization, "itertracks"):
        for turn, _, speaker in diarization.itertracks(yield_label=True):
            segments.append({
                "start": turn.start,
                "end": turn.end,
                "speaker": speaker,
            })
    elif hasattr(diarization, "speaker_diarization"):
        for turn, speaker in diarization.speaker_diarization:
            segments.append({
                "start": turn.start,
                "end": turn.end,
                "speaker": speaker,
            })
    else:
        raise RuntimeError(
            f"Unexpected diarization output type: {type(diarization)}. "
            f"Available attributes: {dir(diarization)}"
        )

    # Count unique speakers
    speakers = set(seg["speaker"] for seg in segments)
    print(f"  Detected {len(speakers)} speaker(s): {', '.join(sorted(speakers))}")

    return segments


# ---------------------------------------------------------------------------
# Merge transcription + diarization
# ---------------------------------------------------------------------------

def assign_speakers_to_segments(whisper_result: dict, diarization_segments: list) -> list:
    """
    Assign speaker labels to Whisper transcript segments by matching timestamps.
    Uses the diarization speaker that has the most overlap with each transcript segment.
    """
    transcript_segments = []

    for segment in whisper_result["segments"]:
        seg_start = segment["start"]
        seg_end = segment["end"]
        seg_text = segment["text"].strip()

        if not seg_text:
            continue

        # Find the diarization speaker with the most overlap
        best_speaker = "Unknown"
        best_overlap = 0.0

        for d_seg in diarization_segments:
            overlap_start = max(seg_start, d_seg["start"])
            overlap_end = min(seg_end, d_seg["end"])
            overlap = max(0.0, overlap_end - overlap_start)

            if overlap > best_overlap:
                best_overlap = overlap
                best_speaker = d_seg["speaker"]

        transcript_segments.append({
            "start": seg_start,
            "end": seg_end,
            "speaker": best_speaker,
            "text": seg_text,
        })

    return transcript_segments


def merge_consecutive_segments(segments: list) -> list:
    """Merge consecutive segments from the same speaker for cleaner output."""
    if not segments:
        return []

    merged = [segments[0].copy()]

    for seg in segments[1:]:
        prev = merged[-1]
        # Merge if same speaker and gap is less than 2 seconds
        if seg["speaker"] == prev["speaker"] and (seg["start"] - prev["end"]) < 2.0:
            prev["end"] = seg["end"]
            prev["text"] += " " + seg["text"]
        else:
            merged.append(seg.copy())

    return merged


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def create_friendly_speaker_names(segments: list) -> dict:
    """Map raw speaker IDs (SPEAKER_00) to friendly names (Speaker 1)."""
    raw_speakers = sorted(set(seg["speaker"] for seg in segments))
    return {raw: f"Speaker {i+1}" for i, raw in enumerate(raw_speakers)}


def write_transcript(segments: list, output_path: str, speaker_map: dict, audio_filename: str):
    """Write the final transcript to a .txt file."""
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(f"Interview Transcript: {audio_filename}\n")
        f.write(f"{'=' * 60}\n")
        f.write(f"Speakers detected: {len(speaker_map)}\n")
        for raw, friendly in speaker_map.items():
            f.write(f"  {friendly} ({raw})\n")
        f.write(f"{'=' * 60}\n\n")

        for seg in segments:
            speaker = speaker_map.get(seg["speaker"], seg["speaker"])
            timestamp = format_timestamp(seg["start"])
            f.write(f"{speaker} [{timestamp}]:\n{seg['text']}\n\n")

    print(f"  Transcript saved to: {output_path}")


def write_json(segments: list, output_path: str, speaker_map: dict):
    """Write raw segment data as JSON for programmatic use."""
    data = {
        "speaker_map": speaker_map,
        "segments": segments,
    }
    with open(output_path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)
    print(f"  JSON data saved to: {output_path}")


# ---------------------------------------------------------------------------
# Main pipeline
# ---------------------------------------------------------------------------

def process_file(audio_path: str, model_name: str, num_speakers: int, hf_token: str, output_dir: str):
    """Full pipeline for a single audio file."""
    audio_path = Path(audio_path)
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    stem = audio_path.stem
    wav_path = output_dir / f"{stem}_temp.wav"
    txt_path = output_dir / f"{stem}_transcript.txt"
    json_path = output_dir / f"{stem}_transcript.json"

    print(f"\n{'=' * 60}")
    print(f"Processing: {audio_path.name}")
    print(f"{'=' * 60}")

    device = get_device()
    start_time = time.time()

    # Step 1: Convert to WAV
    convert_to_wav(str(audio_path), str(wav_path))

    # Step 2: Transcribe with Whisper
    whisper_result = transcribe_audio(str(wav_path), model_name, device)

    # Step 3: Diarize with pyannote
    diarization_segments = diarize_audio(str(wav_path), hf_token, num_speakers, device)

    # Step 4: Merge transcription + diarization
    print(f"  Merging transcription with speaker labels...")
    labeled_segments = assign_speakers_to_segments(whisper_result, diarization_segments)
    merged_segments = merge_consecutive_segments(labeled_segments)

    # Step 5: Write output
    speaker_map = create_friendly_speaker_names(merged_segments)
    write_transcript(merged_segments, str(txt_path), speaker_map, audio_path.name)
    write_json(merged_segments, str(json_path), speaker_map)

    # Cleanup temp WAV
    if wav_path.exists():
        wav_path.unlink()

    elapsed = time.time() - start_time
    print(f"  Completed in {format_timestamp(elapsed)}")


def main():
    parser = argparse.ArgumentParser(
        description="Transcribe interview audio with speaker diarization",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s interview.mp3
  %(prog)s interview.mp3 --model large-v3 --num-speakers 2
  %(prog)s ./interviews/                    # batch process folder
  %(prog)s file.m4a --output ./transcripts/
        """,
    )
    parser.add_argument("input", help="Audio file or directory of audio files")
    parser.add_argument(
        "--model", default="large-v3",
        choices=["tiny", "base", "small", "medium", "large", "large-v2", "large-v3"],
        help="Whisper model size (default: large-v3). Smaller = faster but less accurate.",
    )
    parser.add_argument(
        "--num-speakers", type=int, default=None,
        help="Number of speakers (if known). Improves diarization accuracy.",
    )
    parser.add_argument(
        "--output", default=None,
        help="Output directory (default: same directory as input file)",
    )
    parser.add_argument(
        "--hf-token", default=None,
        help="Hugging Face token (or set HF_TOKEN env variable)",
    )

    args = parser.parse_args()

    # Load .env file (searches current dir and parent dirs)
    load_dotenv()

    # Resolve HF token
    hf_token = args.hf_token or os.environ.get("HF_TOKEN")
    if not hf_token:
        print("ERROR: Hugging Face token required.")
        print("  Set via: export HF_TOKEN='hf_your_token_here'")
        print("  Or pass: --hf-token hf_your_token_here")
        print("\n  Get your token at: https://huggingface.co/settings/tokens")
        sys.exit(1)

    input_path = Path(args.input)

    # Collect files to process
    if input_path.is_dir():
        files = sorted(
            f for f in input_path.iterdir()
            if f.suffix.lower() in SUPPORTED_EXTENSIONS
        )
        if not files:
            print(f"No supported audio files found in {input_path}")
            sys.exit(1)
        print(f"Found {len(files)} audio file(s) to process")
    elif input_path.is_file():
        if input_path.suffix.lower() not in SUPPORTED_EXTENSIONS:
            print(f"Unsupported format: {input_path.suffix}")
            print(f"Supported: {', '.join(sorted(SUPPORTED_EXTENSIONS))}")
            sys.exit(1)
        files = [input_path]
    else:
        print(f"Not found: {input_path}")
        sys.exit(1)

    # Process each file
    for audio_file in files:
        output_dir = args.output or str(audio_file.parent)
        process_file(
            audio_path=str(audio_file),
            model_name=args.model,
            num_speakers=args.num_speakers,
            hf_token=hf_token,
            output_dir=output_dir,
        )

    print(f"\nAll done! Processed {len(files)} file(s).")


if __name__ == "__main__":
    main()
