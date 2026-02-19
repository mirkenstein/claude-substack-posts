#!/usr/bin/env bash
# Transcribe all podcast episodes that don't have transcripts yet.
# Skips episodes that already have episode_transcript.txt.
#
# Usage:
#   ./transcribe_all.sh
#   ./transcribe_all.sh --num-speakers 2    # override default speaker count
#   ./transcribe_all.sh --dry-run            # just list what would be transcribed

set -euo pipefail

AUDIO_DIR="posts/saved/audio"
TRANSCRIBE_SCRIPT="transcribe/transcribe_interview.py"
MODEL="large-v3"
NUM_SPEAKERS=3
DRY_RUN=false

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --num-speakers) NUM_SPEAKERS="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ -z "${HUGGING_FACE_HUB_TOKEN:-}" ]]; then
    echo "ERROR: HUGGING_FACE_HUB_TOKEN not set"
    exit 1
fi

# Collect episodes needing transcription
todo=()
done_count=0
for mp3 in "$AUDIO_DIR"/*/*/episode.mp3; do
    dir=$(dirname "$mp3")
    if [[ -f "$dir/episode_transcript.txt" ]]; then
        done_count=$((done_count + 1))
    else
        todo+=("$mp3")
    fi
done

echo "Already transcribed: $done_count"
echo "To transcribe: ${#todo[@]}"
echo ""

if $DRY_RUN; then
    for mp3 in "${todo[@]}"; do
        echo "  $mp3"
    done
    exit 0
fi

# Transcribe
failed=0
for i in "${!todo[@]}"; do
    mp3="${todo[$i]}"
    dir=$(dirname "$mp3")
    n=$((i + 1))
    echo "[$n/${#todo[@]}] Transcribing: $mp3"

    if python "$TRANSCRIBE_SCRIPT" "$mp3" \
        --num-speakers "$NUM_SPEAKERS" \
        --model "$MODEL" \
        --hf-token "$HUGGING_FACE_HUB_TOKEN"; then
        echo "  -> Done: $dir/episode_transcript.txt"
    else
        echo "  -> FAILED: $mp3"
        failed=$((failed + 1))
    fi
    echo ""
done

echo "Finished. Transcribed: $((${#todo[@]} - failed)), Failed: $failed"
