#!/usr/bin/env python3
"""
Load Telegram Desktop JSON exports into the telegram schema.

Usage:
    python telegram/load_telegram.py telegram/result.json
    python telegram/load_telegram.py telegram/result.json --language ru
    python telegram/load_telegram.py telegram/channel1.json telegram/channel2.json
    python telegram/load_telegram.py telegram/result.json --database podcasts
"""

import argparse
import json
import sys
from pathlib import Path

import psycopg2

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from db.config import get_db_config

NOT_DOWNLOADED = '(File not included. Change data exporting settings to download.)'


def build_text_plain(text_entities):
    """Concatenate text_entities into plain text."""
    if not text_entities:
        return None
    parts = []
    for e in text_entities:
        t = e.get('text', '') if isinstance(e, dict) else str(e)
        parts.append(t)
    return ''.join(parts) or None


def file_path_or_null(val):
    """Return NULL for Telegram's placeholder string."""
    if not val or val == NOT_DOWNLOADED:
        return None
    return val


def load_channel(conn, data, language=None):
    """Load one channel export into the telegram schema."""
    cur = conn.cursor()

    channel_id = data['id']
    channel_name = data['name']
    channel_type = data['type']

    # Upsert channel
    cur.execute("""
        INSERT INTO telegram.channels (id, name, type, language)
        VALUES (%s, %s, %s, %s)
        ON CONFLICT (id) DO NOTHING
    """, (channel_id, channel_name, channel_type, language))

    messages = data.get('messages', [])
    msg_count = 0
    media_count = 0
    link_count = 0
    reaction_count = 0

    for m in messages:
        msg_id = m['id']
        msg_type = m['type']
        posted_at = m.get('date')
        posted_unixtime = m.get('date_unixtime')
        if posted_unixtime:
            posted_unixtime = int(posted_unixtime)

        edited_at = m.get('edited')
        edited_unixtime = m.get('edited_unixtime')
        if edited_unixtime:
            edited_unixtime = int(edited_unixtime)

        text_entities = m.get('text_entities', [])
        text_plain = build_text_plain(text_entities)

        # Standard message fields
        from_name = m.get('from')
        from_id = m.get('from_id')
        forwarded_from = m.get('forwarded_from')
        forwarded_from_id = m.get('forwarded_from_id')

        # Service message fields
        actor = m.get('actor')
        actor_id = m.get('actor_id')
        action = m.get('action')
        action_target = m.get('message_id') if msg_type == 'service' else None

        # Upsert message
        cur.execute("""
            INSERT INTO telegram.messages
                (channel_id, id, type, posted_at, posted_unixtime,
                 edited_at, edited_unixtime, from_name, from_id,
                 forwarded_from, forwarded_from_id, text_plain,
                 actor, actor_id, action, action_target_message_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (channel_id, id) DO UPDATE SET
                text_plain = EXCLUDED.text_plain,
                edited_at = EXCLUDED.edited_at,
                edited_unixtime = EXCLUDED.edited_unixtime
        """, (channel_id, msg_id, msg_type, posted_at, posted_unixtime,
              edited_at, edited_unixtime, from_name, from_id,
              forwarded_from, forwarded_from_id, text_plain,
              actor, actor_id, action, action_target))
        msg_count += 1

        # Media: photo
        if 'photo' in m:
            cur.execute("""
                INSERT INTO telegram.message_media
                    (channel_id, message_id, media_type, file_path,
                     file_size, width, height)
                VALUES (%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (channel_id, msg_id, 'photo',
                  file_path_or_null(m.get('photo')),
                  m.get('photo_file_size'),
                  m.get('width'), m.get('height')))
            media_count += 1

        # Media: video/audio/document (file + media_type)
        if 'media_type' in m and 'photo' not in m:
            cur.execute("""
                INSERT INTO telegram.message_media
                    (channel_id, message_id, media_type, mime_type,
                     file_name, file_size, file_path,
                     width, height, duration_seconds,
                     thumbnail_path, thumbnail_file_size)
                VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (channel_id, msg_id, m['media_type'], m.get('mime_type'),
                  m.get('file_name'), m.get('file_size'),
                  file_path_or_null(m.get('file')),
                  m.get('width'), m.get('height'),
                  m.get('duration_seconds'),
                  file_path_or_null(m.get('thumbnail')),
                  m.get('thumbnail_file_size')))
            media_count += 1

        # Links from text_entities
        for e in text_entities:
            if not isinstance(e, dict):
                continue
            href = e.get('href')
            if not href:
                continue
            cur.execute("""
                INSERT INTO telegram.message_links
                    (channel_id, message_id, link_text, href, entity_type)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (channel_id, msg_id, e.get('text'), href, e.get('type')))
            link_count += 1

        # Reactions
        for r in m.get('reactions', []):
            if not r.get('emoji'):
                continue
            cur.execute("""
                INSERT INTO telegram.message_reactions
                    (channel_id, message_id, emoji, reaction_type, count)
                VALUES (%s,%s,%s,%s,%s)
                ON CONFLICT DO NOTHING
            """, (channel_id, msg_id, r.get('emoji'),
                  r.get('type', 'emoji'), r['count']))
            reaction_count += 1

    conn.commit()
    print(f"  [{channel_name}] {msg_count} messages, {media_count} media, "
          f"{link_count} links, {reaction_count} reactions")
    return msg_count


def main():
    parser = argparse.ArgumentParser(description='Load Telegram JSON exports into PostgreSQL')
    parser.add_argument('files', nargs='+', help='JSON export file(s)')
    parser.add_argument('--language', '-l', help='Language code (ru, en, ...)')
    parser.add_argument('--database', default='substack', help='PostgreSQL database (default: substack)')
    args = parser.parse_args()

    config = get_db_config()
    config['database'] = args.database
    conn = psycopg2.connect(**config)

    total = 0
    for filepath in args.files:
        p = Path(filepath)
        if not p.exists():
            print(f"  SKIP {filepath}: file not found", file=sys.stderr)
            continue

        print(f"Loading {p.name}...")
        with open(p, 'r', encoding='utf-8') as f:
            data = json.load(f)

        total += load_channel(conn, data, language=args.language)

    conn.close()
    print(f"\nTotal: {total} messages loaded")


if __name__ == '__main__':
    main()
