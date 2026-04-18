#!/usr/bin/env python3
"""
Incremental Telegram channel poller using Telethon (MTProto).

Reads last loaded message id per channel from telegram.messages, then fetches
newer messages via Telethon and upserts them into the same schema used by
load_telegram.py. Small photos (<= 8 MB) are downloaded; videos and documents
are recorded as metadata only — use fetch_media.py for those on demand.

Auth:
    Set TELEGRAM_API_ID and TELEGRAM_API_HASH (from https://my.telegram.org).
    First run will prompt for phone + SMS code; session is saved to
    telegram/telethon/.session/poller.session.

Usage:
    python telegram/telethon/poll_channel.py strelkov_i
    python telegram/telethon/poll_channel.py strelkov_i tatarigami_ua
    python telegram/telethon/poll_channel.py strelkov_i --language ru
    python telegram/telethon/poll_channel.py strelkov_i --limit 200
    python telegram/telethon/poll_channel.py strelkov_i --full       # ignore watermark
    python telegram/telethon/poll_channel.py strelkov_i --no-media   # skip photo downloads
"""

import argparse
import asyncio
import os
import sys
from pathlib import Path

import psycopg2
from telethon import TelegramClient
from telethon.tl.types import (
    Channel,
    MessageEntityTextUrl,
    MessageEntityUrl,
    MessageMediaDocument,
    MessageMediaPhoto,
    ReactionEmoji,
)

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(REPO_ROOT))
from db.config import get_db_config

SMALL_PHOTO_LIMIT = 8 * 1024 * 1024
SESSION_DIR = Path(__file__).resolve().parent / ".session"
MEDIA_ROOT = REPO_ROOT / "telegram" / "media"


def get_api_credentials():
    api_id = os.getenv("TELEGRAM_API_ID")
    api_hash = os.getenv("TELEGRAM_API_HASH")
    if not api_id or not api_hash:
        sys.exit("error: set TELEGRAM_API_ID and TELEGRAM_API_HASH (my.telegram.org)")
    return int(api_id), api_hash


def get_last_message_id(cur, channel_id):
    cur.execute(
        "SELECT COALESCE(MAX(id), 0) FROM telegram.messages WHERE channel_id = %s",
        (channel_id,),
    )
    return cur.fetchone()[0]


def upsert_channel(cur, entity, language):
    ch_type = "public_channel" if getattr(entity, "broadcast", False) else "public_supergroup"
    cur.execute(
        """
        INSERT INTO telegram.channels (id, name, type, username, language, description)
        VALUES (%s, %s, %s, %s, %s, %s)
        ON CONFLICT (id) DO UPDATE SET
            name = EXCLUDED.name,
            username = EXCLUDED.username,
            language = COALESCE(EXCLUDED.language, telegram.channels.language)
        """,
        (entity.id, entity.title, ch_type, getattr(entity, "username", None), language, None),
    )


def extract_forward(msg):
    fwd = msg.forward
    if not fwd:
        return None, None
    name = getattr(fwd, "from_name", None)
    from_id = None
    if fwd.from_id is not None:
        from_id = str(fwd.from_id)
    chat = getattr(fwd, "chat", None)
    if not name and chat is not None:
        name = getattr(chat, "title", None) or getattr(chat, "username", None)
    sender = getattr(fwd, "sender", None)
    if not name and sender is not None:
        name = getattr(sender, "username", None) or getattr(sender, "first_name", None)
    return name, from_id


def reaction_rows(msg):
    out = []
    r = getattr(msg, "reactions", None)
    if not r or not r.results:
        return out
    for rc in r.results:
        reaction = rc.reaction
        if isinstance(reaction, ReactionEmoji):
            out.append((reaction.emoticon, "emoji", rc.count))
    return out


def link_rows(msg):
    out = []
    text = msg.message or ""
    for e in msg.entities or []:
        if isinstance(e, MessageEntityTextUrl):
            label = text[e.offset : e.offset + e.length]
            out.append((label, e.url, "text_link"))
        elif isinstance(e, MessageEntityUrl):
            url = text[e.offset : e.offset + e.length]
            out.append((url, url, "link"))
    return out


def classify_media(msg):
    """Return (media_type, mime, file_name, file_size, width, height, duration) or None."""
    media = msg.media
    if media is None:
        return None
    if isinstance(media, MessageMediaPhoto):
        size = 0
        w = h = None
        photo = media.photo
        for s in getattr(photo, "sizes", []) or []:
            sz = getattr(s, "size", None) or getattr(s, "sizes", None)
            if isinstance(sz, list) and sz:
                sz = max(sz)
            if isinstance(sz, int) and sz > size:
                size = sz
            if hasattr(s, "w"):
                w, h = s.w, s.h
        return ("photo", "image/jpeg", None, size or None, w, h, None)
    if isinstance(media, MessageMediaDocument):
        doc = media.document
        if doc is None:
            return None
        mime = doc.mime_type or ""
        fname = None
        w = h = None
        duration = None
        media_type = "document"
        for attr in doc.attributes or []:
            cls = type(attr).__name__
            if cls == "DocumentAttributeFilename":
                fname = attr.file_name
            elif cls == "DocumentAttributeVideo":
                media_type = "video_file"
                w, h = attr.w, attr.h
                duration = int(attr.duration) if attr.duration else None
            elif cls == "DocumentAttributeAudio":
                media_type = "voice_message" if getattr(attr, "voice", False) else "audio_file"
                duration = int(attr.duration) if attr.duration else None
            elif cls == "DocumentAttributeAnimated":
                media_type = "animation"
            elif cls == "DocumentAttributeSticker":
                media_type = "sticker"
        if mime.startswith("video/") and media_type == "document":
            media_type = "video_file"
        if mime.startswith("audio/") and media_type == "document":
            media_type = "audio_file"
        return (media_type, mime or None, fname, doc.size, w, h, duration)
    return None


def upsert_message(cur, channel_id, msg):
    posted_at = msg.date
    posted_unix = int(msg.date.timestamp()) if msg.date else None
    edited_at = msg.edit_date
    edited_unix = int(msg.edit_date.timestamp()) if msg.edit_date else None
    fwd_name, fwd_id = extract_forward(msg)
    from_id = str(msg.sender_id) if msg.sender_id else None
    sender = msg.sender
    from_name = None
    if sender is not None:
        from_name = (
            getattr(sender, "title", None)
            or getattr(sender, "username", None)
            or getattr(sender, "first_name", None)
        )

    msg_type = "service" if msg.action is not None else "message"
    action = type(msg.action).__name__ if msg.action is not None else None

    cur.execute(
        """
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
        """,
        (
            channel_id, msg.id, msg_type, posted_at, posted_unix,
            edited_at, edited_unix, from_name, from_id,
            fwd_name, fwd_id, msg.message or None,
            None, None, action, None,
        ),
    )


def upsert_media(cur, channel_id, msg_id, info, file_path):
    media_type, mime, fname, size, w, h, duration = info
    cur.execute(
        """
        INSERT INTO telegram.message_media
            (channel_id, message_id, media_type, mime_type, file_name,
             file_size, file_path, width, height, duration_seconds)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        ON CONFLICT (channel_id, message_id, media_type) DO UPDATE SET
            file_path = COALESCE(EXCLUDED.file_path, telegram.message_media.file_path),
            file_name = COALESCE(EXCLUDED.file_name, telegram.message_media.file_name),
            file_size = COALESCE(EXCLUDED.file_size, telegram.message_media.file_size)
        """,
        (channel_id, msg_id, media_type, mime, fname, size, file_path, w, h, duration),
    )


def upsert_links_and_reactions(cur, channel_id, msg_id, msg):
    for label, href, etype in link_rows(msg):
        cur.execute(
            """
            INSERT INTO telegram.message_links
                (channel_id, message_id, link_text, href, entity_type)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT DO NOTHING
            """,
            (channel_id, msg_id, label, href, etype),
        )
    for emoji, rtype, count in reaction_rows(msg):
        cur.execute(
            """
            INSERT INTO telegram.message_reactions
                (channel_id, message_id, emoji, reaction_type, count)
            VALUES (%s,%s,%s,%s,%s)
            ON CONFLICT (channel_id, message_id, emoji) DO UPDATE SET
                count = EXCLUDED.count
            """,
            (channel_id, msg_id, emoji, rtype, count),
        )


async def poll_channel(client, conn, channel_ref, language, limit, full, download_photos):
    entity = await client.get_entity(channel_ref)
    if not isinstance(entity, Channel):
        print(f"  SKIP {channel_ref}: not a channel/supergroup")
        return 0

    cur = conn.cursor()
    upsert_channel(cur, entity, language)
    conn.commit()

    min_id = 0 if full else get_last_message_id(cur, entity.id)
    username = getattr(entity, "username", None) or str(entity.id)
    photo_dir = MEDIA_ROOT / username / "photos"
    if download_photos:
        photo_dir.mkdir(parents=True, exist_ok=True)

    print(f"  [{entity.title}] min_id={min_id} limit={limit or 'none'}")

    count = 0
    media_count = 0
    photo_dl = 0
    async for msg in client.iter_messages(entity, min_id=min_id, limit=limit, reverse=True):
        upsert_message(cur, entity.id, msg)

        info = classify_media(msg)
        file_path = None
        if info is not None:
            media_type, _, _, size, *_ = info
            if download_photos and media_type == "photo" and (size or 0) <= SMALL_PHOTO_LIMIT:
                target = photo_dir / f"{msg.id}.jpg"
                if not target.exists():
                    try:
                        await client.download_media(msg, file=str(target))
                        photo_dl += 1
                    except Exception as e:
                        print(f"    ! photo {msg.id}: {e}", file=sys.stderr)
                if target.exists():
                    file_path = str(target.relative_to(REPO_ROOT))
            upsert_media(cur, entity.id, msg.id, info, file_path)
            media_count += 1

        upsert_links_and_reactions(cur, entity.id, msg.id, msg)
        count += 1
        if count % 200 == 0:
            conn.commit()
            print(f"    ... {count} messages")

    conn.commit()
    print(f"  [{entity.title}] +{count} messages, {media_count} media, {photo_dl} photos downloaded")
    return count


async def run(args):
    api_id, api_hash = get_api_credentials()
    SESSION_DIR.mkdir(parents=True, exist_ok=True)
    session_path = str(SESSION_DIR / "poller")

    config = get_db_config()
    config["database"] = args.database
    conn = psycopg2.connect(**config)

    total = 0
    async with TelegramClient(session_path, api_id, api_hash) as client:
        for channel in args.channels:
            try:
                total += await poll_channel(
                    client, conn, channel, args.language,
                    args.limit, args.full, not args.no_media,
                )
            except Exception as e:
                print(f"  ERROR {channel}: {e}", file=sys.stderr)
                conn.rollback()

    conn.close()
    print(f"\nTotal: {total} messages loaded")


def main():
    p = argparse.ArgumentParser(description="Incremental Telegram channel poller (Telethon)")
    p.add_argument("channels", nargs="+", help="Channel username(s) or t.me link(s)")
    p.add_argument("--language", "-l", help="Language code (ru, en, ...)")
    p.add_argument("--database", default="substack")
    p.add_argument("--limit", type=int, help="Max messages per channel per run")
    p.add_argument("--full", action="store_true", help="Ignore watermark, re-fetch from beginning")
    p.add_argument("--no-media", action="store_true", help="Skip photo downloads (metadata only)")
    args = p.parse_args()
    asyncio.run(run(args))


if __name__ == "__main__":
    main()
