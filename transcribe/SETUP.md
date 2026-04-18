# Interview Transcription Tool — Setup Guide

## Quick Start (5 minutes)

### 1. Install dependencies

```bash
pip install openai-whisper pyannote.audio torch torchaudio python-dotenv
```

You also need `ffmpeg` installed (likely already on your system):
```bash
# Debian/Ubuntu
sudo apt install ffmpeg

# Arch
sudo pacman -S ffmpeg

# Check if installed
ffmpeg -version
```

### 2. Accept the pyannote model licenses

Visit **both** of these links and click "Agree":
- https://huggingface.co/pyannote/segmentation-3.0
- https://huggingface.co/pyannote/speaker-diarization-3.1

Also 
Could not download xvec_transform.npz from pyannote/speaker-diarization-community-1.
It might be because the repository is private or gated:

* visit https://hf.co/pyannote/speaker-diarization-community-1 to accept user conditions
* visit https://hf.co/settings/tokens to create an authentication token

### 3. Set your Hugging Face token

Get your token at: https://huggingface.co/settings/tokens

The script supports three ways to provide your token (checked in this order):

**Option A — `.env` file (recommended)**

Create a `.env` file in the same directory as the script:
```
HF_TOKEN=hf_your_token_here
```

This keeps the token out of your shell history and is easy to `.gitignore`.

**Option B — Environment variable**
```bash
export HF_TOKEN="hf_your_token_here"
```
To make it permanent, add the line to your `~/.bashrc` or `~/.zshrc`.

**Option C — Command-line flag**
```bash
python transcribe_interview.py interview.mp3 --hf-token hf_your_token_here
```

### 4. Run it

```bash
# Single file
python transcribe_interview.py interview.mp3

# With known speaker count (improves accuracy)
python transcribe_interview.py interview.mp3 --num-speakers 2

# Batch process an entire folder
python transcribe_interview.py ./interviews/

# Custom output directory
python transcribe_interview.py interview.mp3 --output ./transcripts/
```

---

## Output

For each audio file, you get two files:

- **`filename_transcript.txt`** — Human-readable transcript:
  ```
  Speaker 1 [00:00:12]:
  Tell me about your experience with the project.

  Speaker 2 [00:00:18]:
  I've been working on it for about three years now...
  ```

- **`filename_transcript.json`** — Machine-readable with precise timestamps
  (useful if you want to build further tooling on top)

---

## Model Selection

| Model     | VRAM   | Speed      | Accuracy   | Best for                    |
|-----------|--------|------------|------------|-----------------------------|
| `tiny`    | ~1 GB  | Very fast  | Lower      | Quick previews              |
| `base`    | ~1 GB  | Fast       | Fair       | Draft transcripts           |
| `small`   | ~2 GB  | Moderate   | Good       | Decent quality, limited GPU |
| `medium`  | ~5 GB  | Slower     | Very good  | Good balance                |
| `large-v3`| ~10 GB | Slowest    | Best       | Final transcripts (default) |

If you don't have a GPU, start with `small` or `medium` to test:
```bash
python transcribe_interview.py interview.mp3 --model small --num-speakers 2
```

---

## Tips

- **Always pass `--num-speakers 2`** for interviews — it significantly
  improves diarization accuracy when you know the count.
- **GPU makes a huge difference.** A 1-hour file might take 5-10 min on GPU
  vs 30-60 min on CPU with the large model.
- **Audio quality matters.** Lapel mics or separate tracks per speaker
  produce the best results.
- **Review and rename speakers** in the output file — the tool labels them
  Speaker 1, Speaker 2, etc.
