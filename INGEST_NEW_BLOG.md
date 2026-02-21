# Ingest a New Substack Blog

## Setup

```bash
export BLOG=serborthodox   # subdomain from https://<subdomain>.substack.com
```

---

## Initial Ingest (first time)

### Step 1: List all posts

```bash
python main.py --newsletter https://${BLOG}.substack.com --list --all --output-dir posts/${BLOG}/ -v
```

For paid content, add `--cookies cookies.json`.

### Step 2: Fetch full content + comments

```bash
python main.py --from-list posts/${BLOG}/${BLOG}_posts.json --output-dir posts/${BLOG}/ --resume --delay 30 --jitter 4 -v
```

Re-run with `--resume` to pick up where you left off if interrupted.

### Step 3: Load into PostgreSQL

```bash
python load_posts.py --dir posts/${BLOG}/ --resume --log etl_${BLOG}.log -v
```

### Step 4: Upload to Weaviate

```bash
python weaviate/upload_posts.py --publication ${BLOG}
```

### Verify

```bash
python3 -c "
from src.db.connection import DatabaseConnection
db = DatabaseConnection()
conn = db.connect()
cur = conn.cursor()
cur.execute('''
    SELECT count(*), sum(comment_count), sum(wordcount)
    FROM substack.posts p
    JOIN substack.publications pub ON pub.id = p.publication_id
    WHERE pub.subdomain = '${BLOG}'
''')
posts, comments, words = cur.fetchone()
print(f'Posts: {posts}, Comments: {comments}, Words: {words}')
cur.close()
db.close()
"
```

---

## Incremental Refresh (subsequent runs)

Check for new posts and ingest only what's new:

```bash
python refresh_blog.py ${BLOG}
```

Check more than the default 10 latest posts:

```bash
python refresh_blog.py ${BLOG} --check 20
```

Dry run (show new posts without ingesting):

```bash
python refresh_blog.py ${BLOG} --dry-run
```

Skip Weaviate upload:

```bash
python refresh_blog.py ${BLOG} --skip-weaviate
```

With custom delay between fetches:

```bash
python refresh_blog.py ${BLOG} --delay 30 --jitter 4
```

For paid content:

```bash
python refresh_blog.py ${BLOG} --cookies cookies.json
```

The script archives the old post list as `{blog}_posts.{timestamp}.json` before updating.
