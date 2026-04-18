#!/usr/bin/env python3
"""
Load Apify Twitter scrape datasets into twitter.twitter_tweets.

Adapted from load_twitter_all.py for the "Twitter (X.com) Tweets & Profiles Scraper" actor.

Usage:
    python3 load_twitter_apify.py \
        --db "postgresql://user:pass@host:5432/dbname" \
        main-feed-2024.json threads-feed-2024.json [more files...]

    # Dry run (stats only):
    python3 load_twitter_apify.py --db "..." --dry-run main-feed.json threads-feed.json

    # With backfill (update threading on existing rows):
    python3 load_twitter_apify.py --db "..." --backfill main-feed.json threads-feed.json

Features:
    - Accepts any number of input JSON files
    - Deduplicates across files (first file wins for duplicate tweet IDs)
    - Extracts embedded replyToTweet and quotedTweet as standalone rows
    - Handles retweets correctly (username = original author, user.username = retweeter)
    - ON CONFLICT (tweet_id) DO NOTHING for new inserts
    - --backfill: UPDATE existing rows with threading data (parent_tweet_id, conversation_root_id, etc.)

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


def is_primary_user(username):
    """Check if a username matches the primary author."""
    if not username:
        return False
    return username.lower().lstrip("@") == PRIMARY


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
    user = d.get("user") or {}
    username = d.get("username", "")

    # For retweets, the top-level username is the original author.
    # The retweeter is in user.username. We want is_primary_author
    # to reflect whether Marko retweeted it.
    if d.get("isRetweet") and user.get("username"):
        is_primary = is_primary_user(user.get("username"))
    else:
        is_primary = is_primary_user(username)

    # user_id: prefer tweetUserId, fall back to user.userId
    user_id = d.get("tweetUserId") or user.get("userId")

    return (
        d.get("id"),
        d.get("url", ""),
        user_id,
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
        json.dumps(d, ensure_ascii=False),
    )


def embedded_to_row(obj, scrape_source, obj_type="quoted"):
    """Transform an embedded quotedTweet or replyToTweet → tuple matching COLS.
    obj_type: 'quoted' or 'parent' (affects source label)."""
    obj_id = obj.get("id") or extract_id_from_url(obj.get("url"))
    if not obj_id:
        return None

    username = obj.get("username", "")
    is_primary = is_primary_user(username)

    return (
        obj_id,
        obj.get("url", ""),
        obj.get("userId"),
        username,
        obj.get("fullname"),
        obj.get("text"),
        parse_ts(obj.get("timestamp")),
        # booleans
        obj.get("isReply", False),
        obj.get("isQuote", False),
        obj.get("isRetweet", False),
        obj.get("isPinned", False),
        # threading
        None,  # replying_to
        None,  # parent_tweet_id (we don't have it for embedded)
        extract_id_from_url(obj.get("mainTweetUrl")),
        # engagement
        obj.get("views", 0),
        obj.get("likes", 0),
        obj.get("replies", 0),
        obj.get("retweets", 0),
        obj.get("quotes", 0),
        # media
        flatten_urls(obj.get("links")),
        flatten_urls(obj.get("images")),
        flatten_urls(obj.get("videos")),
        # card
        None, None, None, None, None,
        # quoted
        None, None, None, None, None, None,
        # parent
        None, None, None, None, None, None, None,
        # flags
        obj.get("isMainTweet"),
        obj.get("isRootThread"),
        # meta
        is_primary,
        f"{scrape_source}_embedded_{obj_type}",
        obj.get("verified", False),
        json.dumps(obj, ensure_ascii=False),
    )


# ── User profiles ────────────────────────────────────────────────────

USER_COLS = [
    "user_id", "username", "display_name", "bio", "website", "location",
    "join_date", "verified", "followers", "following",
    "total_tweets", "total_likes", "avatar_url", "banner_url",
    "substack_author_handle",
]


def user_to_row(u):
    username = u.get("username", "")
    is_primary = is_primary_user(username)
    return (
        u.get("userId"),
        username,
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

def source_label(filepath):
    """Generate a scrape_source label from filename."""
    basename = os.path.basename(filepath)
    # Strip numeric prefix and extension
    name = re.sub(r'^\d+_', '', basename)
    name = re.sub(r'\.json$', '', name)
    return f"apify_{name}"


def process_dataset(filepath, seen_ids):
    """Read one JSON file → list of row tuples, deduped against seen_ids."""
    scrape_src = source_label(filepath)

    with open(filepath, "r") as f:
        data = json.load(f)

    rows = []
    stats = {"main": 0, "embedded_qt": 0, "embedded_parent": 0, "primary": 0, "skipped": 0}

    # Main tweets
    for d in data:
        tid = d.get("id")
        if not tid or tid in seen_ids:
            stats["skipped"] += 1
            continue
        seen_ids.add(tid)
        rows.append(tweet_to_row(d, scrape_src))
        stats["main"] += 1

        username = d.get("username", "")
        user = d.get("user") or {}
        if d.get("isRetweet"):
            if is_primary_user(user.get("username")):
                stats["primary"] += 1
        elif is_primary_user(username):
            stats["primary"] += 1

    # Embedded quoted tweets
    for d in data:
        qt = d.get("quotedTweet") or {}
        qt_id = qt.get("id") or extract_id_from_url(qt.get("url"))
        if qt_id and qt_id not in seen_ids and qt.get("text"):
            row = embedded_to_row(qt, scrape_src, "quoted")
            if row:
                seen_ids.add(qt_id)
                rows.append(row)
                stats["embedded_qt"] += 1

    # Embedded parent tweets (replyToTweet)
    for d in data:
        parent = d.get("replyToTweet") or {}
        parent_id = parent.get("id")
        if parent_id and parent_id not in seen_ids and parent.get("text"):
            row = embedded_to_row(parent, scrape_src, "parent")
            if row:
                seen_ids.add(parent_id)
                rows.append(row)
                stats["embedded_parent"] += 1

    return rows, data, stats


def collect_users(all_data):
    """Extract unique user profiles across all datasets."""
    users = {}
    for data in all_data:
        for d in data:
            u = d.get("user")
            if u and u.get("userId") and u["userId"] not in users:
                users[u["userId"]] = u
    return users


def backfill_threading(cur, all_data):
    """Update existing rows with threading data from new scrape.
    For tweets already in DB that have NULL parent_tweet_id or conversation_root_id,
    fill in from the new data where available."""
    threading_map = {}  # tweet_id -> {parent_tweet_id, conversation_root_id, parent_*, quoted_*}

    for data in all_data:
        for d in data:
            tid = d.get("id")
            if not tid:
                continue

            parent = d.get("replyToTweet") or {}
            qt = d.get("quotedTweet") or {}
            card = d.get("card") or {}

            updates = {}
            if parent.get("id"):
                updates["parent_tweet_id"] = parent["id"]
                updates["parent_username"] = parent.get("username")
                updates["parent_text"] = parent.get("text")
                updates["parent_tweet_url"] = parent.get("url")
                updates["parent_likes"] = parent.get("likes")
                updates["parent_views"] = parent.get("views")
            conv_root = extract_id_from_url(d.get("mainTweetUrl"))
            if conv_root:
                updates["conversation_root_id"] = conv_root
            qt_id = extract_id_from_url(qt.get("url"))
            if qt_id:
                updates["quoted_tweet_id"] = qt_id
                updates["quoted_username"] = qt.get("username")
                updates["quoted_text"] = qt.get("text")
                updates["quoted_tweet_url"] = qt.get("url")
            if card.get("url"):
                updates["card_url"] = card.get("url")
                updates["card_title"] = card.get("title")
                updates["card_description"] = card.get("description")
                updates["card_domain"] = card.get("domain")
                updates["card_image_url"] = card.get("image")
            if d.get("isMainTweet") is not None:
                updates["is_main_tweet"] = d["isMainTweet"]
            if d.get("isRootThread") is not None:
                updates["is_root_thread"] = d["isRootThread"]

            replying_to = norm_replying_to(d.get("replyingTo"))
            if replying_to:
                updates["replying_to"] = replying_to

            if updates:
                threading_map[tid] = updates

    if not threading_map:
        print("  No threading data to backfill.")
        return 0

    # Find existing tweets that need backfill
    all_ids = list(threading_map.keys())
    cur.execute("""
        SELECT tweet_id FROM twitter.twitter_tweets
        WHERE tweet_id = ANY(%s)
        AND (parent_tweet_id IS NULL OR conversation_root_id IS NULL
             OR quoted_tweet_id IS NULL OR replying_to IS NULL
             OR is_main_tweet IS NULL)
    """, (all_ids,))
    needs_update = set(row[0] for row in cur.fetchall())

    updated = 0
    for tid in needs_update:
        updates = threading_map.get(tid, {})
        if not updates:
            continue

        # Build SET clause: only update NULL columns
        set_parts = []
        values = []
        for col, val in updates.items():
            if col == "replying_to":
                set_parts.append(f"{col} = COALESCE({col}, %s)")
            else:
                set_parts.append(f"{col} = COALESCE({col}, %s)")
            values.append(val)

        if not set_parts:
            continue

        values.append(tid)
        sql = f"UPDATE twitter.twitter_tweets SET {', '.join(set_parts)} WHERE tweet_id = %s"
        cur.execute(sql, values)
        updated += cur.rowcount

    return updated


def main():
    parser = argparse.ArgumentParser(
        description="Load Apify Twitter datasets into PostgreSQL",
        epilog="Files are processed in order given. First file wins for duplicate tweet IDs."
    )
    parser.add_argument("--db", required=True, help="PostgreSQL connection string")
    parser.add_argument("files", nargs="+", help="JSON files to load (in priority order)")
    parser.add_argument("--batch-size", type=int, default=100, help="Rows per INSERT batch")
    parser.add_argument("--dry-run", action="store_true", help="Print stats but don't insert")
    parser.add_argument("--backfill", action="store_true",
                        help="Also UPDATE existing rows with threading data")
    args = parser.parse_args()

    # Validate files
    for path in args.files:
        if not os.path.exists(path):
            sys.exit(f"File not found: {path}")

    # Process datasets in given order
    seen_ids = set()
    all_rows = []
    all_data = []

    print(f"Processing {len(args.files)} file(s)...")
    for filepath in args.files:
        rows, data, stats = process_dataset(filepath, seen_ids)
        all_rows.extend(rows)
        all_data.append(data)
        label = source_label(filepath)
        embedded = stats["embedded_qt"] + stats["embedded_parent"]
        print(f"  {os.path.basename(filepath)} ({label}):")
        print(f"    {stats['main']} main + {stats['embedded_qt']} embedded_qt + "
              f"{stats['embedded_parent']} embedded_parent = {stats['main']+embedded} rows "
              f"({stats['primary']} Marko, {stats['skipped']} skipped dupes)")

    print(f"\nTotal rows to insert: {len(all_rows)}")
    print(f"Total unique IDs seen: {len(seen_ids)}")

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

    # Get existing count
    cur.execute("SELECT count(*) FROM twitter.twitter_tweets")
    pre_count = cur.fetchone()[0]
    print(f"Existing tweets in DB: {pre_count}")

    # Insert users first
    if users:
        print(f"Inserting {len(users)} user profiles...")
        user_sql = (
            f"INSERT INTO twitter.twitter_users ({','.join(USER_COLS)}) "
            f"VALUES %s ON CONFLICT (user_id) DO NOTHING"
        )
        user_rows = [user_to_row(u) for u in users.values() if u.get("userId")]
        if user_rows:
            execute_values(cur, user_sql, user_rows, page_size=100)
            conn.commit()
            print(f"  Users done ({len(user_rows)} attempted).")

    # Insert tweets in batches
    tweet_sql = (
        f"INSERT INTO twitter.twitter_tweets ({','.join(COLS)}) "
        f"VALUES %s ON CONFLICT (tweet_id) DO NOTHING"
    )

    total = len(all_rows)
    bs = args.batch_size
    errors = 0
    inserted = 0

    print(f"Inserting {total} tweets in batches of {bs}...")
    for i in range(0, total, bs):
        batch = all_rows[i : i + bs]
        try:
            execute_values(cur, tweet_sql, batch, page_size=bs)
            conn.commit()
            inserted += len(batch)
        except Exception as e:
            conn.rollback()
            errors += 1
            print(f"  ERROR batch {i//bs}: {e}")
            # Try row-by-row
            row_errors = 0
            for row in batch:
                try:
                    execute_values(cur, tweet_sql, [row], page_size=1)
                    conn.commit()
                    inserted += 1
                except Exception as e2:
                    conn.rollback()
                    row_errors += 1
                    if row_errors <= 3:
                        print(f"    Skipped tweet {row[0]}: {str(e2)[:100]}")
            if row_errors > 3:
                print(f"    ... and {row_errors - 3} more failures")
            if row_errors:
                print(f"  Batch {i//bs}: {row_errors}/{len(batch)} rows failed")

        done = min(i + bs, total)
        if done % 500 == 0 or done == total:
            print(f"  {done}/{total} ({done*100//total}%)")

    # Backfill threading on existing rows
    backfilled = 0
    if args.backfill:
        print("\nBackfilling threading data on existing rows...")
        backfilled = backfill_threading(cur, all_data)
        conn.commit()
        print(f"  Updated {backfilled} existing rows with threading data.")

    # Verify
    cur.execute("SELECT count(*) FROM twitter.twitter_tweets")
    post_count = cur.fetchone()[0]
    cur.execute("SELECT count(*) FROM twitter.twitter_tweets WHERE is_primary_author = true")
    marko_count = cur.fetchone()[0]
    cur.execute("SELECT min(tweet_date), max(tweet_date) FROM twitter.twitter_tweets")
    date_range = cur.fetchone()
    cur.execute("""
        SELECT scrape_source, count(*)
        FROM twitter.twitter_tweets
        GROUP BY scrape_source ORDER BY scrape_source
    """)
    by_source = cur.fetchall()
    cur.execute("SELECT count(*) FROM twitter.twitter_users")
    user_count = cur.fetchone()[0]

    print(f"\n{'='*55}")
    print(f"LOAD COMPLETE")
    print(f"{'='*55}")
    print(f"Before:             {pre_count} tweets")
    print(f"After:              {post_count} tweets (+{post_count - pre_count} new)")
    print(f"Marko tweets:       {marko_count}")
    print(f"User profiles:      {user_count}")
    print(f"Date range:         {date_range[0]} → {date_range[1]}")
    if backfilled:
        print(f"Threading backfill: {backfilled} rows updated")
    print(f"\nBy source:")
    for source, count in by_source:
        print(f"  {source}: {count}")
    if errors:
        print(f"\nBatch errors (retried row-by-row): {errors}")

    cur.close()
    conn.close()


if __name__ == "__main__":
    main()
