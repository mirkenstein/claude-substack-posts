#!/usr/bin/env bash
# Transcribe all podcast episodes that don't have transcripts yet.
# Skips episodes that already have episode_transcript.txt.
# Speaker count per episode is inferred from the database (title/description heuristics).
#
# Usage:
#   ./transcribe_all.sh
#   ./transcribe_all.sh --subdomain slavlandchronicles   # one publication only
#   ./transcribe_all.sh --dry-run                         # just list what would be transcribed
#   ./transcribe_all.sh --num-speakers 2                  # force override for all episodes

set -euo pipefail

AUDIO_DIR="posts/saved/audio"
TRANSCRIBE_SCRIPT="transcribe/transcribe_interview.py"
MODEL="large-v3"
FORCE_SPEAKERS=""
DRY_RUN=false
SUBDOMAIN_FILTER=""

# Parse arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --num-speakers) FORCE_SPEAKERS="$2"; shift 2 ;;
        --model) MODEL="$2"; shift 2 ;;
        --subdomain) SUBDOMAIN_FILTER="$2"; shift 2 ;;
        --dry-run) DRY_RUN=true; shift ;;
        *) echo "Unknown option: $1"; exit 1 ;;
    esac
done

if [[ -z "${HUGGING_FACE_HUB_TOKEN:-}" ]]; then
    echo "ERROR: HUGGING_FACE_HUB_TOKEN not set"
    exit 1
fi

# ── Build speaker count map from the database ────────────────────────────────

SPEAKER_MAP_FILE=$(mktemp)
trap 'rm -f "$SPEAKER_MAP_FILE"' EXIT

python3 -c "
import re, sys
sys.path.insert(0, '.')
from src.db.connection import DatabaseConnection

db = DatabaseConnection()
conn = db.connect()
cur = conn.cursor()
cur.execute('''
    SELECT p.id, pub.subdomain, p.title, p.description
    FROM substack.posts p
    JOIN substack.publications pub ON pub.id = p.publication_id
    WHERE p.podcast_url IS NOT NULL
''')

for pid, subdomain, title, desc in cur.fetchall():
    title = title or ''
    desc = desc or ''
    t = title.lower()
    d = desc.lower()

    if subdomain == 'anti-empire':
        # WOAW episodes: Marco, Rolo, Slavsquat
        speakers = 3
    elif subdomain == 'edwardslavsquat':
        if re.search(r'rolo.*marko|marko.*rolo|rolo.*edward|marko.*edward', d):
            speakers = 3
        elif re.search(r'[wW]/\s+', title):
            speakers = 2
        elif re.search(r'conversation with|interview with|speaking with', d):
            speakers = 2
        elif re.search(r'a [\w\s]+ (journalist|describes|explains|discusses)', d):
            speakers = 2
        else:
            speakers = 1
    elif subdomain == 'slavlandchronicles':
        if 'worst of all worlds' in t or 'woaw' in t:
            speakers = 3
        elif re.search(r'[wW]/\s+.+\s+and\s+', title):
            speakers = 3
        elif re.search(r'[wW]/\s+', title):
            speakers = 2
        elif 'slavsquat' in t or 'mikovic' in t:
            speakers = 2
        elif 'slavsquat' in d:
            speakers = 2
        else:
            speakers = 1
    else:
        speakers = 1

    # Output: subdomain/post_id<TAB>speakers
    print(f'{subdomain}/{pid}\t{speakers}')

cur.close()
db.close()
" > "$SPEAKER_MAP_FILE"

echo "Loaded speaker counts for $(wc -l < "$SPEAKER_MAP_FILE") episodes from DB"

# ── Collect episodes needing transcription ────────────────────────────────────

todo=()
done_count=0
skip_count=0

for mp3 in "$AUDIO_DIR"/*/*/episode.mp3; do
    dir=$(dirname "$mp3")

    # Filter by subdomain if requested
    if [[ -n "$SUBDOMAIN_FILTER" ]]; then
        # Path is audio/<subdomain>/<post_id>/episode.mp3
        ep_subdomain=$(basename "$(dirname "$dir")")
        if [[ "$ep_subdomain" != "$SUBDOMAIN_FILTER" ]]; then
            skip_count=$((skip_count + 1))
            continue
        fi
    fi

    if [[ -f "$dir/episode_transcript.txt" ]]; then
        done_count=$((done_count + 1))
    else
        todo+=("$mp3")
    fi
done

echo "Already transcribed: $done_count"
[[ -n "$SUBDOMAIN_FILTER" ]] && echo "Filtered out: $skip_count (other subdomains)"
echo "To transcribe: ${#todo[@]}"
echo ""

# ── Lookup helper ─────────────────────────────────────────────────────────────

get_speakers() {
    local mp3="$1"
    # Extract subdomain/post_id from path: audio/<subdomain>/<post_id>/episode.mp3
    local post_id
    post_id=$(basename "$(dirname "$mp3")")
    local subdomain
    subdomain=$(basename "$(dirname "$(dirname "$mp3")")")
    local key="${subdomain}/${post_id}"

    local count
    count=$(grep "^${key}	" "$SPEAKER_MAP_FILE" | cut -f2)
    if [[ -z "$count" ]]; then
        echo "2"  # safe default
    else
        echo "$count"
    fi
}

# ── Dry run ───────────────────────────────────────────────────────────────────

if $DRY_RUN; then
    for mp3 in "${todo[@]}"; do
        if [[ -n "$FORCE_SPEAKERS" ]]; then
            ns="$FORCE_SPEAKERS"
        else
            ns=$(get_speakers "$mp3")
        fi
        echo "  [${ns}p] $mp3"
    done
    exit 0
fi

# ── Transcribe ────────────────────────────────────────────────────────────────

failed=0
for i in "${!todo[@]}"; do
    mp3="${todo[$i]}"
    dir=$(dirname "$mp3")
    n=$((i + 1))

    if [[ -n "$FORCE_SPEAKERS" ]]; then
        ns="$FORCE_SPEAKERS"
    else
        ns=$(get_speakers "$mp3")
    fi

    echo "[$n/${#todo[@]}] Transcribing (${ns} speakers): $mp3"

    if python "$TRANSCRIBE_SCRIPT" "$mp3" \
        --num-speakers "$ns" \
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
