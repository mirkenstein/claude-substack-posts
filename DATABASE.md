# Substack PostgreSQL Database

Store fetched Substack posts, comments, and extracted links in a local PostgreSQL database with automatic link classification.

## Prerequisites

- PostgreSQL running on localhost
- Python 3.12+ with `psycopg2-binary` (included in `requirements.txt`)

## Quick Start

```bash
# 1. Create the database and schema
python db/create_database.py

# 2. Load posts from a directory
python load_posts.py --dir posts/esq/

# 3. Load with ETL logging
python load_posts.py --dir posts/slc/ --log etl_slc.log

# 4. Resume an interrupted load (skips already-loaded files)
python load_posts.py --dir posts/esq/ --resume
```

## Database Setup

### Configuration

Connection defaults in `db/config.py`:

| Setting  | Default     | Env Variable |
|----------|-------------|--------------|
| Host     | `localhost` | `DB_HOST`    |
| Port     | `5432`      | `DB_PORT`    |
| Database | `substack`  | `DB_NAME`    |
| User     | `postgres`  | `DB_USER`    |
| Password | `postgres`  | `DB_PASSWORD`|

Override any setting via environment variables:

```bash
DB_HOST=myserver DB_PASSWORD=secret python load_posts.py --dir posts/esq/
```

### Creating the Database

```bash
python db/create_database.py
```

This creates the `substack` database (if it doesn't exist), the `substack` schema, and runs all migrations from `db/migrations/`.

### Resetting the Database

```bash
psql -h localhost -U postgres -c "DROP DATABASE substack;"
python db/create_database.py
```

## Schema

All tables live in the `substack` schema.

### Tables

| Table | Purpose |
|-------|---------|
| `publications` | Substack publication metadata (subdomain, name, logo) |
| `authors` | Post authors (name, handle, bio, twitter) |
| `posts` | Posts with full HTML content, metadata, and engagement metrics |
| `post_tags` | Tags associated with posts |
| `comments` | Comments with nested threading via `ancestor_path` and computed `depth` |
| `post_links` | Links extracted from post HTML content, classified by source |
| `comment_links` | Links extracted from comment bodies, classified by source |
| `load_status` | Tracks which JSON files have been loaded (enables `--resume`) |

### Link Classification

Links from both post content and comments are automatically classified:

| Category | Domain Patterns |
|----------|----------------|
| `telegram` | `t.me`, `telegram.me` |
| `substack` | `*.substack.com` |
| `youtube` | `youtube.com`, `youtu.be` |
| `rumble` | `rumble.com` |
| `twitter` | `x.com`, `twitter.com` |
| `archive` | `archive.org`, `archive.ph`, `archive.is` |
| `blog` | `blogspot.*`, `wordpress.com`, `medium.com` |
| `news` | Major news outlets (NYT, BBC, Reuters, RT, TASS, etc.) |
| `other` | Everything else |

The classifier is in `src/db/link_classifier.py`. Add domains to `_NEWS_DOMAINS` to recategorize Russian/niche news sites from `other` to `news`.

### Valuable Comments

Comments are flagged `is_valuable = true` when they meet any of:

- 2+ external links in the body
- 10+ reactions
- Body longer than 500 characters AND at least 1 link

### Views

| View | Description |
|------|-------------|
| `posts_with_details` | Posts joined with publication name and author |
| `link_stats_by_domain` | Post link counts grouped by domain and category |
| `comment_link_stats` | Comment link counts grouped by domain and category |
| `valuable_comments` | Valuable comments with post title/slug and link count |

## Loading Data

### CLI Reference

```
python load_posts.py --dir <path>    Load all JSON files from a directory
python load_posts.py --file <path>   Load a single JSON file
                     --resume        Skip files already loaded successfully
                     --log <path>    Write ETL log to file (appends)
                     --verbose       Show skipped files on console
```

### What the Loader Does

For each JSON file:

1. **Upserts publication** from the nested byline metadata
2. **Upserts author** from `publishedBylines[0]`
3. **Upserts post** with full HTML content, metadata, reactions (JSONB)
4. **Inserts tags** from `postTags`
5. **Extracts and inserts post links** from HTML (filters out CDN image URLs)
6. **Recursively inserts comments** preserving thread structure via `ancestor_path`
7. **Extracts and inserts comment links** from each comment body
8. **Flags valuable comments** using the heuristic above
9. **Records load status** for resume support

Each file is loaded in a single transaction. On error, the transaction rolls back and the error is recorded in `load_status`.

### ETL Logging

With `--log`, the log file captures:

- **INFO**: Start/end banners, per-file progress `[+]`/`[!]`
- **DEBUG**: Per-file detail with `post_id`, link count, comment count
- **ERROR**: File read failures and database errors

```bash
# Example: load with logging
python load_posts.py --dir posts/slc/ --log etl_slc.log

# Check the log
tail -5 etl_slc.log
# 2026-02-16 19:01:31 INFO    ETL DONE in 7.0s: 882 loaded, 0 skipped, 0 errors
```

## Querying

### Connect

```bash
psql -h localhost -U postgres -d substack
```

### Example Queries

```sql
-- Posts by publication
SELECT subdomain, count(*) as posts
FROM substack.posts p
JOIN substack.publications pub ON p.publication_id = pub.id
GROUP BY subdomain ORDER BY posts DESC;

-- Top cited domains in post content
SELECT * FROM substack.link_stats_by_domain LIMIT 20;

-- Top cited domains in comments
SELECT * FROM substack.comment_link_stats LIMIT 20;

-- Telegram links across all posts
SELECT p.title, p.slug, pl.url, pl.anchor_text
FROM substack.post_links pl
JOIN substack.posts p ON pl.post_id = p.id
WHERE pl.source_category = 'telegram'
ORDER BY p.post_date DESC;

-- Valuable comments with their links
SELECT v.post_title, v.author_name, v.body, v.reaction_count, v.link_count
FROM substack.valuable_comments v
ORDER BY v.reaction_count DESC
LIMIT 20;

-- Full-text search on post titles
SELECT title, post_date, canonical_url
FROM substack.posts
WHERE to_tsvector('english', title || ' ' || coalesce(subtitle, ''))
      @@ to_tsquery('english', 'Wagner & mutiny');

-- Comment threads with most replies
SELECT c.id, p.title, c.author_name, c.body,
       count(r.id) as replies
FROM substack.comments c
JOIN substack.posts p ON c.post_id = p.id
LEFT JOIN substack.comments r ON r.ancestor_path LIKE c.id::text || '.%'
WHERE c.ancestor_path = ''
GROUP BY c.id, p.title, c.author_name, c.body
ORDER BY replies DESC
LIMIT 10;
```

## File Structure

```
db/
  config.py                          # Connection config (env-overridable)
  create_database.py                 # Creates database + runs migrations
  migrations/
    001_create_schema_and_tables.sql  # Full DDL: tables, indexes, views
src/db/
  __init__.py                        # Module exports
  connection.py                      # DatabaseConnection context manager
  link_classifier.py                 # Domain classification + link extraction
  loader.py                          # PostLoader class (upsert/insert logic)
load_posts.py                        # CLI entry point for ETL
```
