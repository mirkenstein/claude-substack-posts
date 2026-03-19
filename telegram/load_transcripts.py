#!/usr/bin/env python3
"""
Load Telegram audio/video transcript JSON files into the telegram schema.

Matches each transcript to an existing message_media row. Two naming conventions:
  - {message_id}_{description}_transcript.json  (matched by message ID)
  - {original_media_name}_transcript.json        (matched by file_name in DB)

Usage:
    # Single file
    python telegram/load_transcripts.py "telegram/sample_transcript/Intleopap Final New В ТГ_transcript.json"

    # Multiple files
    python telegram/load_transcripts.py transcripts/*_transcript.json

    # All transcript files under a directory (recursive)
    python telegram/load_transcripts.py --dir telegram/sample_transcript/

    # Specify channel to narrow media lookup
    python telegram/load_transcripts.py --channel Papirusvtelege transcript.json

    # Different database
    python telegram/load_transcripts.py --database podcasts transcript.json

    # Dry run (show matches without inserting)
    python telegram/load_transcripts.py --dry-run transcript.json
"""

import argparse
import json
import re
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.config import get_db_config

TRANSCRIPT_SUFFIX = '_transcript.json'

# Pattern: leading digits followed by underscore or end (e.g. "1312_description" or "3656")
MSG_ID_PREFIX_RE = re.compile(r'^(\d+)(?:_|$)')


def extract_media_basename(filepath: Path) -> str | None:
    """Extract the original media base name from a transcript filename.

    'Intleopap Final New В ТГ_transcript.json' -> 'Intleopap Final New В ТГ'
    """
    name = filepath.name
    if name.endswith(TRANSCRIPT_SUFFIX):
        return name[:-len(TRANSCRIPT_SUFFIX)]
    return None


def extract_message_id(media_basename: str) -> int | None:
    """Extract leading message ID from basename if present.

    '1312_moskva_artkorrektировщик' -> 1312
    'IMG_1677' -> None
    """
    m = MSG_ID_PREFIX_RE.match(media_basename)
    return int(m.group(1)) if m else None


def _channel_filter(channel: str | None) -> tuple[str, list]:
    """Build a channel filter clause and params.

    Matches against username, name, or numeric channel ID.
    Returns (sql_fragment, params) where sql_fragment includes leading 'AND'.
    """
    if not channel:
        return "", []
    # Try numeric channel ID
    try:
        channel_id = int(channel)
        return "AND c.id = %s", [channel_id]
    except ValueError:
        pass
    return "AND (c.username = %s OR c.name = %s)", [channel, channel]


def find_media_by_message_id(cur, message_id: int, channel: str | None = None):
    """Find message_media row by message ID directly.

    Returns (media_id, channel_id, message_id) or None.
    """
    ch_filter, ch_params = _channel_filter(channel)
    if ch_filter:
        cur.execute(f"""
            SELECT mm.id, mm.channel_id, mm.message_id
            FROM telegram.message_media mm
            JOIN telegram.channels c ON c.id = mm.channel_id
            WHERE mm.message_id = %s {ch_filter}
            LIMIT 1
        """, [message_id] + ch_params)
    else:
        cur.execute("""
            SELECT mm.id, mm.channel_id, mm.message_id
            FROM telegram.message_media mm
            WHERE mm.message_id = %s
            LIMIT 1
        """, (message_id,))
    return cur.fetchone()


def find_media_by_filename(cur, media_basename: str, channel: str | None = None):
    """Find message_media row matching by file_name (without extension).

    Returns (media_id, channel_id, message_id) or None.
    """
    ch_filter, ch_params = _channel_filter(channel)
    if ch_filter:
        cur.execute(f"""
            SELECT mm.id, mm.channel_id, mm.message_id
            FROM telegram.message_media mm
            JOIN telegram.channels c ON c.id = mm.channel_id
            WHERE (
                regexp_replace(mm.file_name, '\\.[^.]+$', '') = %s
                OR regexp_replace(mm.file_path, '^.*/', '') = %s
                   || '.' || split_part(mm.file_name, '.', -1)
            )
            {ch_filter}
            LIMIT 1
        """, [media_basename, media_basename] + ch_params)
    else:
        cur.execute("""
            SELECT mm.id, mm.channel_id, mm.message_id
            FROM telegram.message_media mm
            WHERE regexp_replace(mm.file_name, '\\.[^.]+$', '') = %s
               OR regexp_replace(mm.file_path, '^.*/', '') = %s
                  || '.' || split_part(mm.file_name, '.', -1)
            LIMIT 1
        """, (media_basename, media_basename))
    return cur.fetchone()


def find_media_row(cur, media_basename: str, channel: str | None = None):
    """Find the message_media row matching a transcript's base filename.

    Matching strategy (in order):
    1. If basename starts with digits_ (e.g. '1312_description'), look up by message ID
    2. Fall back to file_name matching (without extension)

    Returns (media_id, channel_id, message_id) or None.
    """
    # Strategy 1: message ID prefix
    msg_id = extract_message_id(media_basename)
    if msg_id is not None:
        match = find_media_by_message_id(cur, msg_id, channel)
        if match:
            return match

    # Strategy 2: filename match
    return find_media_by_filename(cur, media_basename, channel)


def load_transcript(conn, filepath: Path, channel: str | None = None,
                    dry_run: bool = False) -> bool:
    """Load a single transcript JSON file into the database.

    Returns True if successfully loaded (or matched in dry-run mode).
    """
    media_basename = extract_media_basename(filepath)
    if media_basename is None:
        print(f"  SKIP {filepath.name}: does not end with '{TRANSCRIPT_SUFFIX}'",
              file=sys.stderr)
        return False

    with open(filepath, 'r', encoding='utf-8') as f:
        data = json.load(f)

    segments = data.get('segments', [])
    speaker_map = data.get('speaker_map')

    if not segments:
        print(f"  SKIP {filepath.name}: no segments", file=sys.stderr)
        return False

    cur = conn.cursor()

    # Find matching media row
    match = find_media_row(cur, media_basename, channel)
    if match is None:
        print(f"  SKIP {filepath.name}: no matching media for '{media_basename}'",
              file=sys.stderr)
        return False

    media_id, channel_id, message_id = match

    if dry_run:
        print(f"  MATCH {filepath.name} -> media_id={media_id}, "
              f"channel_id={channel_id}, msg_id={message_id} "
              f"({len(segments)} segments)")
        return True

    # Check for existing transcript (delete and re-insert for idempotency)
    cur.execute("""
        SELECT id FROM telegram.media_transcripts
        WHERE media_id = %s
    """, (media_id,))
    existing = cur.fetchone()
    if existing:
        transcript_id = existing[0]
        cur.execute("DELETE FROM telegram.media_transcript_segments WHERE transcript_id = %s",
                    (transcript_id,))
        cur.execute("DELETE FROM telegram.media_transcripts WHERE id = %s",
                    (transcript_id,))

    # Concatenate full transcript text
    full_text = '\n'.join(seg['text'] for seg in segments)

    # Detect language from speaker_map / content heuristics
    # Default to 'ru' since the sample is Russian
    language = None

    # Infer transcript_type from presence of speaker data
    has_speakers = any(seg.get('speaker') for seg in segments)
    transcript_type = 'diarized' if has_speakers else 'plain'

    # Insert transcript header
    cur.execute("""
        INSERT INTO telegram.media_transcripts
            (channel_id, media_id, transcript_type, language,
             transcript_text, speaker_map, model_version)
        VALUES (%s, %s, %s, %s, %s, %s, %s)
        RETURNING id
    """, (channel_id, media_id, transcript_type, language,
          full_text, json.dumps(speaker_map) if speaker_map else None, None))
    transcript_id = cur.fetchone()[0]

    # Insert segments
    for i, seg in enumerate(segments):
        cur.execute("""
            INSERT INTO telegram.media_transcript_segments
                (transcript_id, segment_index, start_seconds, end_seconds,
                 speaker, text)
            VALUES (%s, %s, %s, %s, %s, %s)
        """, (transcript_id, i,
              seg.get('start'), seg.get('end'),
              seg.get('speaker'), seg['text']))

    conn.commit()
    speaker_info = f", {len(speaker_map)} speakers" if speaker_map else ""
    print(f"  LOADED {filepath.name} -> media_id={media_id}, msg_id={message_id}: "
          f"{len(segments)} segments{speaker_info}")
    return True


def find_transcript_files(directory: Path) -> list[Path]:
    """Recursively find all *_transcript.json files under a directory."""
    return sorted(directory.rglob(f'*{TRANSCRIPT_SUFFIX}'))


def main():
    parser = argparse.ArgumentParser(
        description='Load Telegram transcript JSON files into PostgreSQL')
    parser.add_argument('files', nargs='*', help='Transcript JSON file(s)')
    parser.add_argument('--dir', '-d', help='Directory to scan for *_transcript.json files')
    parser.add_argument('--channel', '-c',
                        help='Telegram channel (username, name, or numeric ID) to narrow media lookup')
    parser.add_argument('--database', default='substack',
                        help='PostgreSQL database (default: substack)')
    parser.add_argument('--dry-run', action='store_true',
                        help='Show matches without inserting')
    args = parser.parse_args()

    # Collect files
    files = []
    if args.dir:
        files.extend(find_transcript_files(Path(args.dir)))
    for f in (args.files or []):
        files.append(Path(f))

    if not files:
        parser.error('No transcript files specified. Use positional args or --dir.')

    config = get_db_config()
    config['database'] = args.database
    conn = psycopg2.connect(**config)

    loaded = 0
    skipped = 0
    for filepath in files:
        if not filepath.exists():
            print(f"  SKIP {filepath}: file not found", file=sys.stderr)
            skipped += 1
            continue
        if load_transcript(conn, filepath, channel=args.channel,
                           dry_run=args.dry_run):
            loaded += 1
        else:
            skipped += 1

    conn.close()
    action = "Matched" if args.dry_run else "Loaded"
    print(f"\n{action}: {loaded}, Skipped: {skipped}")


if __name__ == '__main__':
    main()