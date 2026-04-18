#!/usr/bin/env python3
"""
Load 3 Apify Twitter scrape datasets into twitter.twitter_tweets.

Usage:
    python3 load_twitter_all.py \
        --db "postgresql://user:pass@host:5432/dbname" \
        --d1 dataset_twitter-scraper_2026-02-20_05-29-55-458.json \
        --d2 dataset_twitter-scraper_2026-02-20_06-48-42-608.json \
        --d3 dataset_twitter-scraper_2026-02-20_07-47-30-127.json

Load order: D1 (richest, conversation scrape) → D3 (hybrid w/ threading) → D2 (flat timeline).
ON CONFLICT (tweet_id) DO NOTHING ensures first-loaded wins (richest data preserved).

Requires: pip install psycopg2-binary
"""

import argparse
import json
import os
import re
import sys
from datetime import datetime

try:
    import psycopg2
    from psycopg2.extras import execute_values
except ImportError:
    sys.exit("psycopg2 not installed. Run: pip install psycopg2-binary")

PRIMARY = "marmar_ae"

# ── Helpers ──────────────────────────────────────────────────────────

def parse_ts(ts):
    """Parse ISO timestamp → datetime or None."""
    if not ts:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%S.%fZ", "%Y-%m-%dT%H:%M:%SZ"):
        try:
            return datetime.strptime(ts, fmt)
        except ValueError:
            pass
    return None


def extract_id_from_url(url):
    """Pull tweet ID from a twitter status URL."""
    if not url:
        return None
    m = re.search(r"/status/(\d+)", url)
    return m.group(1) if m else None


def flatten_urls(lst):
    """Normalize a list that may contain strings or {url:...} dicts → list[str] or None."""
    if not lst:
        return None
    out = []
    for x in lst:
        if x is None:
            continue
        if isinstance(x, str):
            out.append(x)
        elif isinstance(x, dict):
            out.append(x.get("url", ""))
    return out or None


def norm_replying_to(lst):
    """Normalize replyingTo handles: strip leading '/' and ensure '@' prefix."""
    if not lst:
        return None
    return ["@" + r.lstrip("/") if r.startswith("/") else r for r in lst]


# ── Transform ────────────────────────────────────────────────────────

COLS = [
    "tweet_id", "url", "user_id", "username", "display_name", "content", "tweet_date",
    "is_reply", "is_quote", "is_retweet", "is_pinned",
    "replying_to", "parent_tweet_id", "conversation_root_id",
    "views", "likes", "reply_count", "retweets", "quote_count",
    "links", "image_urls", "video_urls",
    "card_url", "card_title", "card_description", "card_domain", "card_image_url",
    "quoted_tweet_id", "quoted_username", "quoted_text", "quoted_tweet_url",
    "quoted_links", "quoted_image_urls",
    "parent_username", "parent_text", "parent_tweet_url",
    "parent_links", "parent_image_urls", "parent_likes", "parent_views",
    "is_main_tweet", "is_root_thread",
    "is_primary_author", "scrape_source", "verified", "raw_json",
]


def tweet_to_row(d, scrape_source):
    """Transform one Apify tweet dict → tuple matching COLS."""
    parent = d.get("replyToTweet") or {}
    qt = d.get("quotedTweet") or {}
    card = d.get("card") or {}
    username = d.get("username", "")
    is_primary = username.lower().lstrip("@") == PRIMARY

    return (
        d.get("id"),
        d.get("url", ""),
        d.get("tweetUserId") or (d.get("user") or {}).get("userId"),
        username,
        d.get("fullname"),
        d.get("text"),
        parse_ts(d.get("timestamp")),
        # booleans
        d.get("isReply", False),
        d.get("isQuote", False),
        d.get("isRetweet", False),
        d.get("isPinned", False),
        # threading
        norm_replying_to(d.get("replyingTo")),
        parent.get("id"),
        extract_id_from_url(d.get("mainTweetUrl")),
        # engagement
        d.get("views", 0),
        d.get("likes", 0),
        d.get("replies", 0),
        d.get("retweets", 0),
        d.get("quotes", 0),
        # media
        flatten_urls(d.get("links")),
        flatten_urls(d.get("images")),
        flatten_urls(d.get("videos")),
        # card
        card.get("url"),
        card.get("title"),
        card.get("description"),
        card.get("domain"),
        card.get("image"),
        # quoted tweet
        extract_id_from_url(qt.get("url")),
        qt.get("username"),
        qt.get("text"),
        qt.get("url"),
        flatten_urls(qt.get("links")),
        flatten_urls(qt.get("images")),
        # parent tweet
        parent.get("username"),
        parent.get("text"),
        parent.get("url"),
        flatten_urls(parent.get("links")),
        flatten_urls(parent.get("images")),
        parent.get("likes"),
        parent.get("views"),
        # flags
        d.get("isMainTweet"),
        d.get("isRootThread"),
        # meta
        is_primary,
        scrape_source,
        d.get("verified", False),
        json.dumps(d, ensure_ascii=False),  # raw_json
    )


def embedded_qt_to_row(qt, scrape_source):
    """Transform an embedded quotedTweet object → tuple matching COLS."""
    qt_id = extract_id_from_url(qt.get("url"))
    if not qt_id:
        return None

    return (
        qt_id,
        qt.get("url", ""),
        qt.get("userId"),
        qt.get("username", ""),
        qt.get("fullname"),
        qt.get("text"),
        parse_ts(qt.get("timestamp")),
        # booleans
        False, False, False, False,
        # threading
        None, None, None,
        # engagement
        0, 0, 0, 0, 0,
        # media
        flatten_urls(qt.get("links")),
        flatten_urls(qt.get("images")),
        None,
        # card
        None, None, None, None, None,
        # quoted
        None, None, None, None, None, None,
        # parent
        None, None, None, None, None, None, None,
        # flags
        None, None,
        # meta
        False,
        scrape_source,
        False,
        json.dumps(qt, ensure_ascii=False),
    )


# ── User profiles ────────────────────────────────────────────────────

USER_COLS = [
    "user_id", "username", "display_name", "bio", "website", "location",
    "join_date", "verified", "followers", "following",
    "total_tweets", "total_likes", "avatar_url", "banner_url",
    "substack_author_handle",
]


def user_to_row(u):
    is_primary = u.get("username", "").lower().lstrip("@") == PRIMARY
    return (
        u.get("userId"),
        u.get("username"),
        u.get("userFullName"),
        u.get("description"),
        u.get("website"),
        u.get("location"),
        parse_ts(u.get("joinDate")),
        u.get("verified", False),
        u.get("totalFollowers"),
        u.get("totalFollowing"),
        u.get("totalTweets"),
        u.get("totalLikes"),
        u.get("avatar"),
        u.get("banner"),
        "antiempire" if is_primary else None,
    )


# ── Load logic ───────────────────────────────────────────────────────

def process_dataset(filepath, scrape_source, seen_ids):
    """Read one JSON file → list of row tuples, deduped against seen_ids."""
    with open(filepath, "r") as f:
        data = json.load(f)

    rows = []
    stats = {"main": 0, "embedded": 0, "primary": 0}

    # Main tweets
    for d in data:
        tid = d.get("id")
        if not tid or tid in seen_ids:
            continue
        seen_ids.add(tid)
        rows.append(tweet_to_row(d, scrape_source))
        stats["main"] += 1
        if d.get("username", "").lower().lstrip("@") == PRIMARY:
            stats["primary"] += 1

    # Embedded quoted tweets (not already in the dataset)
    for d in data:
        qt = d.get("quotedTweet") or {}
        qt_id = extract_id_from_url(qt.get("url"))
        if qt_id and qt_id not in seen_ids:
            row = embedded_qt_to_row(qt, scrape_source)
            if row:
                seen_ids.add(qt_id)
                rows.append(row)
                stats["embedded"] += 1

    return rows, data, stats


def collect_users(datasets):
    """Extract unique user profiles across all datasets."""
    users = {}
    for data in datasets:
        for d in data:
            u = d.get("user")
            if u and u.get("userId") and u["userId"] not in users:
                users[u["userId"]] = u
    return users


def main():
    parser = argparse.ArgumentParser(description="Load Twitter datasets into PostgreSQL")
    parser.add_argument("--db", required=True, help="PostgreSQL connection string")
    parser.add_argument("--d1", required=True, help="Dataset 1 JSON (conversation scrape, Jun-Jul 2025)")
    parser.add_argument("--d2", required=True, help="Dataset 2 JSON (flat timeline, Nov 2024-Jul 2025)")
    parser.add_argument("--d3", required=True, help="Dataset 3 JSON (hybrid, Jan-Jul 2025)")
    parser.add_argument("--batch-size", type=int, default=100, help="Rows per INSERT batch (default: 100)")
    parser.add_argument("--dry-run", action="store_true", help="Print stats but don't insert")
    args = parser.parse_args()

    # Validate files exist
    for label, path in [("D1", args.d1), ("D2", args.d2), ("D3", args.d3)]:
        if not os.path.exists(path):
            sys.exit(f"File not found: {path}")

    # Process datasets in priority order: D1 (richest) → D3 (threading) → D2 (fills gaps)
    seen_ids = set()
    all_rows = []
    all_data = []

    datasets = [
        (args.d1, "marmar_ae_d1_conversation", "D1"),
        (args.d3, "marmar_ae_d3_hybrid",       "D3"),
        (args.d2, "marmar_ae_d2_timeline",      "D2"),
    ]

    print("Processing datasets (load order: D1 → D3 → D2)...")
    for filepath, source, label in datasets:
        rows, data, stats = process_dataset(filepath, source, seen_ids)
        all_rows.extend(rows)
        all_data.append(data)
        print(f"  {label}: {stats['main']} main + {stats['embedded']} embedded "
              f"= {stats['main']+stats['embedded']} rows ({stats['primary']} Marko)")

    print(f"\nTotal rows to insert: {len(all_rows)}")
    print(f"Total unique IDs: {len(seen_ids)}")

    # Collect user profiles
    users = collect_users(all_data)
    print(f"User profiles: {len(users)}")

    if args.dry_run:
        print("\n[DRY RUN] No database changes made.")
        return

    # Connect and insert
    print(f"\nConnecting to database...")
    conn = psycopg2.connect(args.db)
    conn.autocommit = False
    cur = conn.cursor()

    # Insert users first
    if users:
        print(f"Inserting {len(users)} user profiles...")
        user_sql = (
            f"INSERT INTO twitter.twitter_users ({','.join(USER_COLS)}) "
            f"VALUES %s ON CONFLICT (user_id) DO NOTHING"
        )
        user_rows = [user_to_row(u) for u in users.values()]
        execute_values(cur, user_sql, user_rows, page_size=100)
        conn.commit()
        print(f"  Users done.")

    # Insert tweets in batches
    tweet_sql = (
        f"INSERT INTO twitter.twitter_tweets ({','.join(COLS)}) "
        f"VALUES %s ON CONFLICT (tweet_id) DO NOTHING"
    )

    total = len(all_rows)
    bs = args.batch_size
    errors = 0

    print(f"Inserting {total} tweets in batches of {bs}...")
    for i in range(0, total, bs):
        batch = all_rows[i : i + bs]
        try:
            execute_values(cur, tweet_sql, batch, page_size=bs)
            conn.commit()
        except Exception as e:
            conn.rollback()
            errors += 1
            print(f"  ERROR batch {i//bs}: {e}")
            # Try row-by-row for this batch
            row_errors = 0
            for row in batch:
                try:
                    execute_values(cur, tweet_sql, [row], page_size=1)
                    conn.commit()
                except Exception as e2:
                    conn.rollback()
                    row_errors += 1
                    print(f"    Skipped tweet {row[0]}: {str(e2)[:80]}")
            if row_errors:
                print(f"  Batch {i//bs}: {row_errors}/{len(batch)} rows failed")

        done = min(i + bs, total)
        if done % 500 == 0 or done == total:
            print(f"  {done}/{total} ({done*100//total}%)")

    # Verify
    cur.execute("SELECT count(*) FROM twitter.twitter_tweets")
    final_count = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM twitter.twitter_tweets WHERE is_primary_author = true")
    marko_count = cur.fetchone()[0]
    cur.execute("SELECT min(tweet_date), max(tweet_date) FROM twitter.twitter_tweets")
    date_range = cur.fetchone()
    cur.execute("SELECT scrape_source, count(*) FROM twitter.twitter_tweets GROUP BY scrape_source ORDER BY scrape_source")
    by_source = cur.fetchall()

    print(f"\n{'='*50}")
    print(f"LOAD COMPLETE")
    print(f"{'='*50}")
    print(f"Total tweets in DB: {final_count}")
    print(f"Marko tweets:       {marko_count}")
    print(f"Date range:         {date_range[0]} → {date_range[1]}")
    print(f"\nBy source:")
    for source, count in by_source:
        print(f"  {source}: {count}")
    if errors:
        print(f"\nBatch errors (retried row-by-row): {errors}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
