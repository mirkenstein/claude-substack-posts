#!/usr/bin/env python3
"""
On-demand Telegram media fetcher.

Downloads specific messages' media (typically videos or large documents that
poll_channel.py skips) and updates telegram.message_media.file_path.

Usage:
    # By explicit message IDs
    python telegram/telethon/fetch_media.py --channel strelkov_i --ids 12345,12346

    # Feed IDs from a SQL query (one per line on stdin)
    psql -d substack -Atc "SELECT message_id FROM telegram.message_media \
        WHERE channel_id = 123 AND media_type = 'video_file' AND file_path IS NULL" \
      | python telegram/telethon/fetch_media.py --channel strelkov_i --stdin
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import psycopg2
from telethon import TelegramClient

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from db.config import get_db_config

SESSION_DIR = Path(__file__).resolve().parent / ".session"
MEDIA_ROOT = REPO_ROOT / "telegram" / "media"


def get_api_credentials():
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        sys.exit("error: set TELEGRAM_API_ID and TELEGRAM_API_HASH")
    return int(api_id), api_hash


def media_subdir(msg):
    doc = getattr(msg, "document", None)
    if doc:
        mime = (doc.mime_type or "").lower()
        if mime.startswith("video/"):
            return "videos"
        if mime.startswith("audio/"):
            return "audio"
        return "documents"
    if getattr(msg, "photo", None):
        return "photos"
    return "other"


async def fetch(args):
    api_id, api_hash = get_api_credentials()
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_path = str(SESSION_DIR / "poller")

    if args.stdin:
        ids = [int(x.strip()) for x in sys.stdin if x.strip()]
    else:
        ids = [int(x) for x in args.ids.split(",") if x.strip()]
    if not ids:
        sys.exit("error: no message IDs provided")

    config = get_db_config()
    config["database"] = args.database
    conn = psycopg2.connect(**config)
    cur = conn.cursor()

    async with TelegramClient(session_path, api_id, api_hash) as client:
        entity = await client.get_entity(args.channel)
        username = getattr(entity, "username", None) or str(entity.id)

        messages = await client.get_messages(entity, ids=ids)
        for msg in messages:
            if msg is None:
                continue
            if msg.media is None:
                print(f"  {msg.id}: no media, skip")
                continue

            subdir = media_subdir(msg)
            out_dir = MEDIA_ROOT / username / subdir
            out_dir.mkdir(parents=True, exist_ok=True)

            fname = None
            doc = getattr(msg, "document", None)
            if doc:
                for a in doc.attributes or []:
                    if type(a).__name__ == "DocumentAttributeFilename":
                        fname = a.file_name
                        break
            target = out_dir / (f"{msg.id}_{fname}" if fname else f"{msg.id}.bin")

            size_mb = (doc.size / 1024 / 1024) if doc else 0
            print(f"  {msg.id}: {subdir}/{target.name} ({size_mb:.1f} MB)")

            if target.exists() and not args.force:
                print(f"    already downloaded, skip")
            else:
                try:
                    await client.download_media(msg, file=str(target))
                except Exception as e:
                    print(f"    ! download failed: {e}", file=sys.stderr)
                    continue

            rel = str(target.relative_to(REPO_ROOT))
            cur.execute(
                """
                UPDATE telegram.message_media
                SET file_path = %s
                WHERE channel_id = %s AND message_id = %s AND file_path IS NULL
                """,
                (rel, entity.id, msg.id),
            )
            conn.commit()

    conn.close()


def main():
    p = argparse.ArgumentParser(description="Fetch individual Telegram media on demand")
    p.add_argument("--channel", required=True, help="Channel username or t.me link")
    p.add_argument("--ids", help="Comma-separated message IDs")
    p.add_argument("--stdin", action="store_true", help="Read message IDs from stdin (one per line)")
    p.add_argument("--database", default="substack")
    p.add_argument("--force", action="store_true", help="Re-download even if file exists")
    args = p.parse_args()
    if not args.ids and not args.stdin:
        p.error("provide --ids or --stdin")
    asyncio.run(fetch(args))


if __name__ == "__main__":
    main()
