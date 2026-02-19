#!/usr/bin/env python3
"""
Load Substack posts from browser-saved HTML files into PostgreSQL.

Designed for Anti-Empire posts saved as HTML ("Save As" from browser).
Extracts article body, images, and links, then upserts into the substack schema.

Usage:
    python load_html_posts.py --dir /path/to/AE/
    python load_html_posts.py --dir /path/to/AE/ --resume
    python load_html_posts.py --dir /path/to/AE/ --log etl_ae.log
"""

import argparse
import json
import logging
import sys
import time
from pathlib import Path

from src.db.connection import DatabaseConnection
from src.db.loader import PostLoader
from src.html_parser import parse_html_file, copy_images, rewrite_image_srcs

# Anti-Empire constants
PUBLICATION_ID = 810635
AUTHOR_ID = 75334913
AUTHOR_NAME = 'Marko Marjanović'
PUBLICATION_SUBDOMAIN = 'anti-empire'
PUBLICATION_NAME = 'Anti-Empire'
CUSTOM_DOMAIN = 'anti-empire.org'


def setup_logging(log_file: str | None) -> logging.Logger:
    logger = logging.getLogger('etl_html')
    logger.setLevel(logging.DEBUG)
    fmt = logging.Formatter('%(asctime)s %(levelname)-7s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    if log_file:
        fh = logging.FileHandler(log_file, mode='a', encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


def load_metadata_sources(saved_dir: Path) -> tuple[dict, dict]:
    """Load anti-empire_posts.json and list.json for metadata lookup.

    Returns:
        posts_by_slug: slug -> dict from anti-empire_posts.json
        list_by_id: post_id -> dict from list.json
    """
    posts_by_slug = {}
    ae_path = saved_dir / 'anti-empire_posts.json'
    if ae_path.exists():
        data = json.loads(ae_path.read_text(encoding='utf-8'))
        for p in data.get('posts', []):
            slug = p.get('slug') or ''
            if not slug and p.get('url'):
                slug = p['url'].rstrip('/').rsplit('/', 1)[-1]
            if slug:
                posts_by_slug[slug] = p

    list_by_id = {}
    list_path = saved_dir / 'list.json'
    if list_path.exists():
        data = json.loads(list_path.read_text(encoding='utf-8'))
        for p in data.get('posts', []):
            if p.get('id'):
                list_by_id[p['id']] = p

    return posts_by_slug, list_by_id


def _normalize_title(title: str) -> str:
    """Strip 'by Author - Publication' suffixes from browser tab titles."""
    import re
    # Remove trailing " - by Author Name - Publication" or " - by Author Name"
    title = re.sub(r'\s*-\s*by\s+.*$', '', title)
    return title.lower().strip()


def find_slug_for_title(title: str, posts_by_slug: dict) -> str | None:
    """Fuzzy match a title against the slug index."""
    norm = _normalize_title(title)
    for slug, p in posts_by_slug.items():
        if _normalize_title(p.get('title', '')) == norm:
            return slug
    return None


def build_metadata(post_id: int | None, title: str, posts_by_slug: dict, list_by_id: dict) -> dict:
    """Assemble metadata dict expected by PostLoader.upsert_post()."""
    # Start with list.json data (richest) if we have an ID
    list_data = list_by_id.get(post_id, {}) if post_id else {}

    # Find slug from list data or by title match
    slug = list_data.get('slug', '')
    if not slug:
        slug = find_slug_for_title(title, posts_by_slug) or ''

    # Get anti-empire_posts.json data by slug
    ae_data = posts_by_slug.get(slug, {})

    # If we still don't have a post_id, try to find it from list_by_id via slug
    if not post_id:
        for pid, ld in list_by_id.items():
            if ld.get('slug') == slug:
                post_id = pid
                list_data = ld
                break

    metadata = {
        'id': post_id,
        'publication_id': PUBLICATION_ID,
        'slug': slug,
        'title': list_data.get('title') or ae_data.get('title') or title,
        'subtitle': list_data.get('subtitle') or ae_data.get('subtitle'),
        'canonical_url': f'https://{CUSTOM_DOMAIN}/p/{slug}' if slug else '',
        'post_date': list_data.get('post_date') or ae_data.get('date'),
        'updated_at': list_data.get('updated_at'),
        'type': list_data.get('type', 'newsletter'),
        'audience': list_data.get('audience') or ae_data.get('audience'),
        'wordcount': list_data.get('wordcount'),
        'restacks': list_data.get('restacks', 0),
        'comment_count': list_data.get('comment_count', 0),
        'cover_image': list_data.get('cover_image'),
        'description': list_data.get('description'),
        'reactions': list_data.get('reactions'),
    }
    return metadata


def upsert_publication_and_author(loader: PostLoader):
    """Upsert the Anti-Empire publication and author once."""
    with loader.conn.cursor() as cur:
        cur.execute("""
            INSERT INTO substack.publications (id, subdomain, name, custom_domain, logo_url, hero_text)
            VALUES (%s, %s, %s, %s, NULL, NULL)
            ON CONFLICT (id) DO UPDATE SET
                name = EXCLUDED.name,
                custom_domain = EXCLUDED.custom_domain
            RETURNING id
        """, (PUBLICATION_ID, PUBLICATION_SUBDOMAIN, PUBLICATION_NAME, CUSTOM_DOMAIN))

        cur.execute("""
            INSERT INTO substack.authors (id, name, handle, photo_url, bio, twitter_screen_name, bestseller_tier)
            VALUES (%s, %s, %s, NULL, NULL, NULL, NULL)
            ON CONFLICT (id) DO UPDATE SET name = EXCLUDED.name
            RETURNING id
        """, (AUTHOR_ID, AUTHOR_NAME, 'antiempire'))

    loader.conn.commit()


def load_one_html(loader: PostLoader, conn, html_path: Path, posts_by_slug: dict,
                  list_by_id: dict, image_dest: Path, resume: bool, log: logging.Logger) -> str:
    """Load a single HTML file. Returns 'success', 'skipped', or 'error'."""
    path_str = str(html_path)

    if resume and loader.is_loaded(path_str):
        log.debug("SKIP %s (already loaded)", html_path.name)
        return 'skipped'

    try:
        parsed = parse_html_file(html_path)
    except Exception as e:
        log.error("PARSE %s: %s", html_path.name, e)
        loader.record_status(path_str, None, 'error', f'parse: {e}')
        conn.commit()
        return 'error'

    if parsed is None:
        log.error("PARSE %s: no body markup div found", html_path.name)
        loader.record_status(path_str, None, 'error', 'no body markup div')
        conn.commit()
        return 'error'

    post_id = parsed['post_id']
    title = parsed['title']
    content_html = parsed['content_html']
    images = parsed['images']

    metadata = build_metadata(post_id, title, posts_by_slug, list_by_id)

    if metadata['id'] is None:
        log.error("NOID %s: could not determine post ID", html_path.name)
        loader.record_status(path_str, None, 'error', 'no post ID')
        conn.commit()
        return 'error'

    post_id = metadata['id']

    # Copy images and rewrite src paths
    if images:
        rewrite_map = copy_images(html_path, images, post_id, image_dest)
        content_html = rewrite_image_srcs(content_html, rewrite_map)

    try:
        loader.upsert_post(metadata, content_html, False, PUBLICATION_ID, AUTHOR_ID)
        link_count = loader.insert_post_links(post_id, content_html)
        loader.record_status(path_str, post_id, 'success')
        conn.commit()
        log.debug("OK   %s  post_id=%s  links=%s  images=%s",
                   html_path.name, post_id, link_count, len(images))
        return 'success'
    except Exception as e:
        conn.rollback()
        log.error("LOAD %s: %s", html_path.name, e)
        try:
            loader.record_status(path_str, None, 'error', str(e))
            conn.commit()
        except Exception:
            conn.rollback()
        return 'error'


def main():
    parser = argparse.ArgumentParser(description='Load Substack HTML posts into PostgreSQL')
    parser.add_argument('--dir', required=True, help='Directory containing HTML files')
    parser.add_argument('--resume', action='store_true', help='Skip already-loaded files')
    parser.add_argument('--log', help='Path to ETL log file')
    parser.add_argument('--verbose', '-v', action='store_true')
    args = parser.parse_args()

    log = setup_logging(args.log)

    html_dir = Path(args.dir)
    files = sorted(html_dir.glob('*.html'))

    if not files:
        log.error("No HTML files found in %s", html_dir)
        sys.exit(1)

    saved_dir = Path('posts/saved')
    image_dest = saved_dir / 'images'
    image_dest.mkdir(parents=True, exist_ok=True)

    posts_by_slug, list_by_id = load_metadata_sources(saved_dir)
    log.info("Metadata: %d slugs from anti-empire_posts.json, %d posts from list.json",
             len(posts_by_slug), len(list_by_id))

    log.info("=" * 70)
    log.info("ETL START  source=%s  files=%d  resume=%s", html_dir, len(files), args.resume)
    log.info("=" * 70)

    counts = {'success': 0, 'skipped': 0, 'error': 0}
    start = time.time()

    with DatabaseConnection() as conn:
        loader = PostLoader(conn)
        upsert_publication_and_author(loader)

        for i, fp in enumerate(files, 1):
            result = load_one_html(loader, conn, fp, posts_by_slug, list_by_id,
                                   image_dest, args.resume, log)
            counts[result] += 1

            if result == 'skipped' and not args.verbose:
                continue

            symbol = {'success': '+', 'skipped': '-', 'error': '!'}[result]
            log.info("  [%s] %d/%d %s", symbol, i, len(files), fp.name)

    elapsed = time.time() - start
    log.info("-" * 70)
    log.info("ETL DONE in %.1fs: %d loaded, %d skipped, %d errors",
             elapsed, counts['success'], counts['skipped'], counts['error'])
    log.info("=" * 70)


if __name__ == '__main__':
    main()
