#!/usr/bin/env python3
"""Download post media (images, audio, cover images) from Substack posts.

Usage:
    python download_media.py --extract                    # populate post_media table
    python download_media.py --download                   # download all pending media
    python download_media.py --download --type image      # images only
    python download_media.py --download --type audio      # audio only
    python download_media.py --download --publication kk  # one publication
    python download_media.py --extract --download         # both steps
    python download_media.py --fetch-podcasts             # fetch podcast_url from API
    python download_media.py --adopt-podcasts             # adopt existing podcast files
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from src.db.connection import DatabaseConnection
from src.media_extractor import (
    adopt_existing_podcasts,
    download_pending,
    extract_cover_image,
    extract_images_from_html,
    extract_podcast_audio,
    fetch_podcast_urls,
    insert_media_rows,
)

# Publication subdomain aliases for convenience
PUB_ALIASES = {
    "ae": "anti-empire",
    "esq": "edwardslavsquat",
    "slc": "slavlandchronicles",
    "kk": "kamilkazani",
    "drl": "drlivci",
    "woaw": "woaw",
}


def do_extract(conn):
    """Extract media URLs from all posts and insert into post_media."""
    with conn.cursor() as cur:
        cur.execute("""
            SELECT p.id, p.content_html, p.cover_image, p.podcast_url,
                   pub.subdomain
            FROM substack.posts p
            JOIN substack.publications pub ON pub.id = p.publication_id
            ORDER BY p.id
        """)
        posts = cur.fetchall()

    print(f"Scanning {len(posts)} posts for media...")
    total_inserted = 0

    for post_id, content_html, cover_image, podcast_url, subdomain in posts:
        rows = []

        # Inline images from HTML
        rows.extend(extract_images_from_html(post_id, content_html))

        # Cover image
        cover = extract_cover_image(post_id, cover_image)
        if cover:
            rows.append(cover)

        # Podcast audio
        audio = extract_podcast_audio(post_id, podcast_url)
        if audio:
            rows.append(audio)

        if rows:
            inserted = insert_media_rows(conn, rows)
            total_inserted += inserted

    conn.commit()
    print(f"Inserted {total_inserted} new media rows.")

    # Show summary
    with conn.cursor() as cur:
        cur.execute("""
            SELECT media_type, count(*),
                   count(*) FILTER (WHERE downloaded_at IS NOT NULL) as downloaded
            FROM substack.post_media
            GROUP BY media_type
            ORDER BY media_type
        """)
        print("\nMedia summary:")
        print(f"  {'type':<15} {'total':>8} {'downloaded':>12}")
        for mtype, total, dl in cur.fetchall():
            print(f"  {mtype:<15} {total:>8} {dl:>12}")


def do_fetch_podcasts(conn, cookies_path: str):
    """Fetch podcast_url from the Substack API for podcast posts."""
    updated = fetch_podcast_urls(conn, cookies_path)
    print(f"\nUpdated {updated} podcast URLs.")


def do_adopt(conn):
    """Adopt existing podcast files from posts/saved/podcasts/."""
    adopted = adopt_existing_podcasts(conn)
    print(f"Adopted {adopted} existing podcast files.")


def do_download(conn, media_type: str | None, publication: str | None):
    """Download pending media files."""
    # Resolve publication alias
    if publication and publication in PUB_ALIASES:
        publication = PUB_ALIASES[publication]

    downloaded = download_pending(conn, media_type=media_type,
                                  publication=publication)
    print(f"\nDownloaded {downloaded} files.")


def main():
    parser = argparse.ArgumentParser(description="Download media from Substack posts")
    parser.add_argument("--extract", action="store_true",
                        help="Extract media URLs from posts into post_media table")
    parser.add_argument("--download", action="store_true",
                        help="Download all pending media files")
    parser.add_argument("--fetch-podcasts", action="store_true",
                        help="Fetch podcast_url from Substack API for podcast posts")
    parser.add_argument("--adopt-podcasts", action="store_true",
                        help="Adopt existing podcast files from posts/saved/podcasts/")
    parser.add_argument("--type", choices=["image", "audio", "cover_image"],
                        help="Only download this media type")
    parser.add_argument("--publication", "-p",
                        help="Only download for this publication (subdomain or alias)")
    parser.add_argument("--cookies", default="cookies.json",
                        help="Path to cookies JSON file (default: cookies.json)")

    args = parser.parse_args()

    if not any([args.extract, args.download, args.fetch_podcasts, args.adopt_podcasts]):
        parser.print_help()
        sys.exit(1)

    db = DatabaseConnection()
    try:
        conn = db.connect()

        if args.fetch_podcasts:
            do_fetch_podcasts(conn, args.cookies)

        if args.extract:
            do_extract(conn)

        if args.adopt_podcasts:
            do_adopt(conn)

        if args.download:
            do_download(conn, args.type, args.publication)

    finally:
        db.close()


if __name__ == "__main__":
    main()
