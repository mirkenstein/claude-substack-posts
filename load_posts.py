#!/usr/bin/env python3
"""
Load Substack posts from JSON files into PostgreSQL.

Usage:
    python load_posts.py --dir posts/esq/
    python load_posts.py --dir posts/slc/ --log etl.log
    python load_posts.py --file posts/esq/some-post.json
    python load_posts.py --dir posts/esq/ --resume
"""

import argparse
import json
import logging
import sys
import time
from datetime import datetime
from pathlib import Path

from src.db.connection import DatabaseConnection
from src.db.loader import PostLoader


def setup_logging(log_file: str | None) -> logging.Logger:
    """Configure logging to console and optionally to a file."""
    logger = logging.getLogger('etl')
    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter('%(asctime)s %(levelname)-7s %(message)s', datefmt='%Y-%m-%d %H:%M:%S')

    # Console: INFO and above
    console = logging.StreamHandler(sys.stdout)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File: DEBUG and above (captures everything)
    if log_file:
        fh = logging.FileHandler(log_file, mode='a', encoding='utf-8')
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

    return logger


def load_one_file(loader: PostLoader, conn, file_path: Path, resume: bool, log: logging.Logger) -> str:
    """Load a single JSON file. Returns 'success', 'skipped', or 'error'."""
    path_str = str(file_path)

    if resume and loader.is_loaded(path_str):
        log.debug("SKIP %s (already loaded)", file_path.name)
        return 'skipped'

    try:
        data = json.loads(file_path.read_text(encoding='utf-8'))
    except (json.JSONDecodeError, OSError) as e:
        log.error("READ %s: %s", file_path.name, e)
        loader.record_status(path_str, None, 'error', str(e))
        conn.commit()
        return 'error'

    metadata = data.get('metadata', {})
    content_html = data.get('content', '')
    is_paywalled = data.get('is_paywalled', False)
    comments = data.get('comments', [])

    try:
        pub_id = loader.upsert_publication(metadata)
        author_id = loader.upsert_author(metadata)
        post_id = loader.upsert_post(metadata, content_html, is_paywalled, pub_id, author_id)
        loader.insert_tags(post_id, metadata)
        link_count = loader.insert_post_links(post_id, content_html)
        comment_count = loader.insert_comments(post_id, comments)
        loader.record_status(path_str, post_id, 'success')
        conn.commit()
        log.debug("OK   %s  post_id=%s  links=%s  comments=%s",
                   file_path.name, post_id, link_count, comment_count)
        return 'success'
    except Exception as e:
        conn.rollback()
        log.error("LOAD %s: %s", file_path.name, e)
        try:
            loader.record_status(path_str, None, 'error', str(e))
            conn.commit()
        except Exception:
            conn.rollback()
        return 'error'


def main():
    parser = argparse.ArgumentParser(description='Load Substack posts into PostgreSQL')
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--dir', help='Directory containing JSON files')
    group.add_argument('--file', help='Single JSON file to load')
    parser.add_argument('--resume', action='store_true', help='Skip already-loaded files')
    parser.add_argument('--log', help='Path to ETL log file')
    parser.add_argument('--verbose', '-v', action='store_true')
    parser.add_argument('--database', default='substack',
                        help='PostgreSQL database to load into (default: substack)')
    args = parser.parse_args()

    log = setup_logging(args.log)

    if args.dir:
        files = sorted(Path(args.dir).glob('*.json'))
    else:
        files = [Path(args.file)]

    if not files:
        log.error("No JSON files found")
        sys.exit(1)

    source_dir = args.dir or str(Path(args.file).parent)
    log.info("=" * 70)
    log.info("ETL START  source=%s  files=%d  resume=%s", source_dir, len(files), args.resume)
    log.info("=" * 70)

    counts = {'success': 0, 'skipped': 0, 'error': 0}
    start = time.time()

    with DatabaseConnection(database=args.database) as conn:
        loader = PostLoader(conn)

        for i, fp in enumerate(files, 1):
            result = load_one_file(loader, conn, fp, args.resume, log)
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
