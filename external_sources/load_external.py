#!/usr/bin/env python3
"""
Load external articles from JSON files into the external schema.

Usage:
    python external_sources/load_external.py                          # load all JSON files
    python external_sources/load_external.py --file katysha_articles.json
    python external_sources/load_external.py --recreate               # drop & recreate schema, then load
    python external_sources/load_external.py --recreate --no-load     # just recreate schema
"""

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
import psycopg2
import psycopg2.errors

DB_URL = "postgresql://postgres:postgres@localhost:5432/substack"
SCRAPED_DIR = Path(__file__).parent / "scraped_data"
SQL_FILE = Path(__file__).parent / "create_external_tables.sql"

# ═══════════════════════════════════════════════════════════
# Filename → domain mapping
# ═══════════════════════════════════════════════════════════

FILENAME_DOMAIN_MAP = {
    'topwar':       'topwar.ru',
    'topcor':       'topcor.ru',
    'katysha':      'katyusha.org',
    'livejournal':  'livejournal.com',
    'live_journal': 'livejournal.com',
    'WSJ':          'wsj.com',
    'wsj':          'wsj.com',
    'washpost':     'washingtonpost.com',
    'the_nation':   'thenation.com',
}

# Files that are archive recoveries
RECOVERY_FILES = {
    'topwar_archive_articles.json': 'wayback',
    'live_journal_other_articles.json': 'lj_mirror',
}

# ═══════════════════════════════════════════════════════════
# Date parsing — handles all known formats
# ═══════════════════════════════════════════════════════════

RUSSIAN_MONTHS = {
    'января': 1, 'февраля': 2, 'марта': 3, 'апреля': 4,
    'мая': 5, 'июня': 6, 'июля': 7, 'августа': 8,
    'сентября': 9, 'октября': 10, 'ноября': 11, 'декабря': 12,
}

ENGLISH_MONTHS = {
    'january': 1, 'february': 2, 'march': 3, 'april': 4,
    'may': 5, 'june': 6, 'july': 7, 'august': 8,
    'september': 9, 'october': 10, 'november': 11, 'december': 12,
}

def parse_date(date_str):
    """Parse various date formats to datetime or None."""
    if not date_str or not date_str.strip():
        return None
    date_str = date_str.strip()

    # ISO format: 2017-06-23T15:00 or 2024-02-10T18:07:00Z
    if re.match(r'^\d{4}-\d{2}-\d{2}', date_str):
        try:
            date_str = date_str.replace('Z', '+00:00')
            if 'T' in date_str:
                for fmt in ('%Y-%m-%dT%H:%M:%S%z', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%dT%H:%M'):
                    try:
                        return datetime.strptime(date_str.split('+')[0].split('Z')[0], fmt.replace('%z', '')).replace(tzinfo=timezone.utc)
                    except ValueError:
                        continue
            return datetime.strptime(date_str[:10], '%Y-%m-%d').replace(tzinfo=timezone.utc)
        except ValueError:
            pass

    # English: "June 20 2020, 08:18" or "October 30 1978, 00:00"
    m = re.match(r'(\w+)\s+(\d{1,2})\s+(\d{4}),?\s*(\d{2}:\d{2})?', date_str)
    if m:
        month_name, day, year = m.group(1).lower(), int(m.group(2)), int(m.group(3))
        time_str = m.group(4)
        month = ENGLISH_MONTHS.get(month_name)
        if month:
            hour, minute = (0, 0)
            if time_str:
                hour, minute = map(int, time_str.split(':'))
            try:
                return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
            except ValueError:
                pass

    # Russian: "24 Ноября 2021 в 02:00"
    m = re.match(r'(\d{1,2})\s+(\w+)\s+(\d{4})\s*(?:в\s*)?(\d{2}:\d{2})?', date_str)
    if m:
        day, month_name, year = int(m.group(1)), m.group(2).lower(), int(m.group(3))
        time_str = m.group(4)
        month = RUSSIAN_MONTHS.get(month_name)
        if month:
            hour, minute = (0, 0)
            if time_str:
                hour, minute = map(int, time_str.split(':'))
            try:
                return datetime(year, month, day, hour, minute, tzinfo=timezone.utc)
            except ValueError:
                pass

    # Relative dates like "Сегодня, 04:17" — can't resolve, return None
    return None


def parse_views(views_str):
    """Parse TopWar view counts like '15 680' to int."""
    if not views_str:
        return None
    try:
        return int(str(views_str).replace(' ', '').replace('\xa0', '').strip())
    except (ValueError, TypeError):
        return None


def parse_comment_count(count_str):
    """Parse comment count from header field."""
    if not count_str:
        return None
    try:
        return int(str(count_str).replace(' ', '').strip())
    except (ValueError, TypeError):
        return None


def extract_slug(url, file_path):
    """Extract article slug from URL or file path."""
    if url:
        m = re.search(r'/([^/]+?)(?:\.html)?$', url)
        if m:
            return m.group(1)
    if file_path:
        m = re.search(r'([^/]+?)(?:\.html)?$', file_path)
        if m:
            return m.group(1)
    return None


def is_404_article(article):
    """Check if article is a 404 (no text, no title, no url)."""
    return not article.get('text') and not article.get('title') and not article.get('url')


def guess_domain(filename):
    """Guess source domain from filename prefix."""
    name = Path(filename).stem  # e.g. 'topwar_articles' or 'topwar_new_articles'
    # Try longest prefix match first
    for prefix in sorted(FILENAME_DOMAIN_MAP.keys(), key=len, reverse=True):
        if name.startswith(prefix):
            return FILENAME_DOMAIN_MAP[prefix]
    return None


def ensure_source(cur, domain):
    """Ensure source exists in external.sources, auto-create if missing."""
    cur.execute("SELECT id FROM external.sources WHERE domain = %s", (domain,))
    row = cur.fetchone()
    if row:
        return row[0]
    # Auto-create with minimal info
    cur.execute(
        "INSERT INTO external.sources (domain, name, language) VALUES (%s, %s, 'unknown') RETURNING id",
        (domain, domain)
    )
    source_id = cur.fetchone()[0]
    print(f"  Created new source: {domain} (id={source_id})")
    return source_id


# ═══════════════════════════════════════════════════════════
# Schema management
# ═══════════════════════════════════════════════════════════

def recreate_schema(conn):
    """Drop and recreate the external schema from SQL file."""
    if not SQL_FILE.exists():
        print(f"ERROR: SQL file not found: {SQL_FILE}", file=sys.stderr)
        sys.exit(1)

    sql = SQL_FILE.read_text(encoding='utf-8')
    cur = conn.cursor()

    print("Dropping external schema...")
    cur.execute("DROP SCHEMA IF EXISTS external CASCADE")
    conn.commit()

    print(f"Recreating from {SQL_FILE.name}...")
    cur.execute(sql)
    conn.commit()
    print("Schema recreated.")


# ═══════════════════════════════════════════════════════════
# Loader
# ═══════════════════════════════════════════════════════════

def load_json_file(conn, filepath, recovery_source=None):
    """Load articles from a JSON file. Domain is inferred from filename."""
    filepath = Path(filepath)
    domain = guess_domain(filepath.name)
    if not domain:
        print(f"  SKIP {filepath.name}: cannot determine source domain from filename")
        return 0, 0

    with open(filepath, 'r') as f:
        data = json.load(f)

    articles = data.get('articles', data) if isinstance(data, dict) else data

    cur = conn.cursor()
    source_id = ensure_source(cur, domain)

    mark_deleted = 'archive' in filepath.stem
    article_count = 0
    comment_count = 0

    for a in articles:
        url = a.get('url') or None
        file_path = a.get('file') or None
        title = a.get('title') or None
        subtitle = a.get('subtitle') or None
        author = a.get('author') or None
        text = a.get('text') or None
        publish_date = parse_date(a.get('date'))
        views = parse_views(a.get('views'))
        likes_val = a.get('likes')
        likes = int(likes_val) if likes_val and str(likes_val).strip().isdigit() else None
        cc = parse_comment_count(a.get('comments'))
        slug = extract_slug(url, file_path)
        source_article_id = a.get('source_article_id') or None

        is_deleted = mark_deleted or is_404_article(a)

        # Clean author field (some TopWar entries have URLs instead of names)
        if author and author.startswith('http'):
            author = None

        image_urls = json.dumps(a.get('image_urls') or [])
        external_links = json.dumps(a.get('external_links') or [])
        internal_links = json.dumps(a.get('internal_links') or [])

        # Skip articles without a URL
        if not url:
            continue

        # Skip the opinions.html junk entry
        if file_path and 'opinions.html' in file_path and not text:
            continue

        upsert_values = (source_id, source_article_id, url, slug, file_path, title, subtitle, author,
                         publish_date, text, views, likes, cc,
                         image_urls, external_links, internal_links,
                         recovery_source, is_deleted)

        try:
            cur.execute("""
                INSERT INTO external.articles
                    (source_id, source_article_id, url, slug, file_path, title, subtitle, author,
                     publish_date, text, views, likes, comment_count,
                     image_urls, external_links, internal_links,
                     recovery_source, is_deleted)
                VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (source_id, url) DO UPDATE SET
                    source_article_id = COALESCE(EXCLUDED.source_article_id, external.articles.source_article_id),
                    file_path = COALESCE(EXCLUDED.file_path, external.articles.file_path),
                    slug = COALESCE(EXCLUDED.slug, external.articles.slug),
                    title = COALESCE(EXCLUDED.title, external.articles.title),
                    subtitle = COALESCE(EXCLUDED.subtitle, external.articles.subtitle),
                    author = COALESCE(EXCLUDED.author, external.articles.author),
                    publish_date = COALESCE(EXCLUDED.publish_date, external.articles.publish_date),
                    text = COALESCE(EXCLUDED.text, external.articles.text),
                    views = COALESCE(EXCLUDED.views, external.articles.views),
                    likes = COALESCE(EXCLUDED.likes, external.articles.likes),
                    comment_count = COALESCE(EXCLUDED.comment_count, external.articles.comment_count),
                    image_urls = EXCLUDED.image_urls,
                    external_links = EXCLUDED.external_links,
                    internal_links = EXCLUDED.internal_links,
                    recovery_source = COALESCE(EXCLUDED.recovery_source, external.articles.recovery_source),
                    is_deleted = EXCLUDED.is_deleted,
                    updated_at = now()
                RETURNING id
            """, upsert_values)

            article_id = cur.fetchone()[0]
            article_count += 1

            # Load comments — delete existing first to avoid duplicates on re-run
            comments_list = a.get('comments_list') or []
            if comments_list and isinstance(comments_list, list):
                cur.execute("DELETE FROM external.comments WHERE article_id = %s", (article_id,))
                for c in comments_list:
                    if not isinstance(c, dict):
                        continue
                    body = c.get('text') or ''
                    if not body.strip():
                        continue
                    cur.execute("""
                        INSERT INTO external.comments
                            (article_id, source_comment_id, username, comment_date, body, rating)
                        VALUES (%s, %s, %s, %s, %s, %s)
                    """, (
                        article_id,
                        c.get('id'),
                        c.get('user'),
                        c.get('date'),
                        body,
                        c.get('rating')
                    ))
                    comment_count += 1

        except Exception as e:
            print(f"  Error loading article '{title or file_path}': {e}")
            conn.rollback()
            source_id = ensure_source(cur, domain)
            continue

    conn.commit()
    print(f"  [{domain}] {article_count} articles, {comment_count} comments from {filepath.name}")
    return article_count, comment_count


# ═══════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(description='Load external articles into PostgreSQL')
    parser.add_argument('--file', help='Load a single JSON file from scraped_data/')
    parser.add_argument('--recreate', action='store_true', help='Drop and recreate external schema before loading')
    parser.add_argument('--no-load', action='store_true', help='With --recreate, only recreate schema without loading data')
    args = parser.parse_args()

    conn = psycopg2.connect(DB_URL)

    if args.recreate:
        recreate_schema(conn)
        if args.no_load:
            conn.close()
            return

    # Determine which files to load
    if args.file:
        file_path = SCRAPED_DIR / args.file
        if not file_path.exists():
            print(f"ERROR: File not found: {file_path}", file=sys.stderr)
            sys.exit(1)
        json_files = [file_path]
    else:
        json_files = sorted(SCRAPED_DIR.glob('*.json'))

    if not json_files:
        print("No JSON files found in scraped_data/")
        sys.exit(1)

    print(f"Loading {len(json_files)} JSON files from {SCRAPED_DIR}/\n")

    total_articles = 0
    total_comments = 0

    for filepath in json_files:
        recovery = RECOVERY_FILES.get(filepath.name)
        try:
            a, c = load_json_file(conn, filepath, recovery_source=recovery)
            total_articles += a
            total_comments += c
        except Exception as e:
            print(f"  FAILED {filepath.name}: {e}")

    print(f"\n{'='*60}")
    print(f"TOTAL: {total_articles} articles, {total_comments} comments")

    # Summary by source
    cur = conn.cursor()
    cur.execute("""
        SELECT s.domain, count(*), sum(CASE WHEN a.text IS NOT NULL THEN 1 ELSE 0 END)
        FROM external.articles a
        JOIN external.sources s ON s.id = a.source_id
        GROUP BY s.domain ORDER BY count(*) DESC
    """)
    print(f"\n{'Domain':<25} {'Articles':>10} {'With Text':>10}")
    print(f"{'-'*25} {'-'*10} {'-'*10}")
    for domain, count, with_text in cur.fetchall():
        print(f"{domain:<25} {count:>10} {with_text:>10}")

    conn.close()


if __name__ == '__main__':
    main()
