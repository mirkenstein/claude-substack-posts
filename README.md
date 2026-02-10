# Substack Article Fetcher

Fetch Substack articles and comments programmatically. Supports authenticated access for paid subscriber content.

## Installation

```bash
# Requires Python 3.12+
python3.12 -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt
```

## Quick Start

### Fetch a public post with comments

```bash
python main.py --url https://newsletter.substack.com/p/post-slug
```

### Fetch paid/paywalled content

You need to provide your session cookies from a logged-in browser session.

1. **Export your cookies:**
   - Log into Substack in your browser
   - Open Developer Tools (F12) > Application > Cookies
   - Find cookies for `.substack.com`
   - Copy `substack.sid`, `substack.lli`, and `connect.sid` values
   - Create `cookies.json` based on `cookies.example.json`

2. **Fetch with authentication:**
   ```bash
   python main.py --url https://newsletter.substack.com/p/paid-post --cookies cookies.json
   ```

## Command Line Arguments

### Input Options

| Argument | Description |
|----------|-------------|
| `--url URL` | URL of a specific Substack post to fetch |
| `--newsletter URL` | URL of a Substack newsletter to fetch multiple posts |
| `--from-list FILE` | Path to a posts list JSON file (from `--list --output-dir`) |

### Output Options

| Argument | Description |
|----------|-------------|
| `--output`, `-o FILE` | Output file path for single post (default: stdout) |
| `--output-dir DIR` | Output directory for saving JSON files |

### Authentication

| Argument | Description |
|----------|-------------|
| `--cookies FILE` | Path to cookies JSON file for authenticated access |

### Fetch Options

| Argument | Default | Description |
|----------|---------|-------------|
| `--limit N` | 10 | Maximum number of posts to fetch |
| `--all` | - | Fetch all posts (overrides `--limit`) |
| `--list` | - | List posts without fetching content |
| `--no-comments` | - | Don't fetch comments |
| `--resume` | - | Skip posts that already exist in output directory |

### Date Filtering

| Argument | Description |
|----------|-------------|
| `--since YYYY-MM-DD` | Only include posts from this date onwards |
| `--until YYYY-MM-DD` | Only include posts up to this date |

### Rate Limiting

| Argument | Default | Description |
|----------|---------|-------------|
| `--delay N` | 2.0 | Delay in seconds between requests |
| `--jitter N` | 1.0 | Random jitter (0 to N seconds) added to delay |

### Debug

| Argument | Description |
|----------|-------------|
| `--verbose`, `-v` | Enable verbose output for debugging |

## Usage Examples

### Fetch a single post

```bash
# Output to stdout
python main.py --url https://newsletter.substack.com/p/post-slug

# Save to file
python main.py --url https://newsletter.substack.com/p/post-slug --output post.json

# Without comments
python main.py --url https://newsletter.substack.com/p/post-slug --no-comments

# With authentication for paid content
python main.py --url https://newsletter.substack.com/p/paid-post --cookies cookies.json
```

### List posts from a newsletter

```bash
# List latest 10 posts (JSON to stdout)
python main.py --newsletter https://newsletter.substack.com --list

# List all posts and save to file
python main.py --newsletter https://newsletter.substack.com --list --all --output-dir ./posts

# List posts from a specific year
python main.py --newsletter https://newsletter.substack.com --list --all --since 2024-01-01 --until 2024-12-31

# List posts from 2025 onwards
python main.py --newsletter https://newsletter.substack.com --list --all --since 2025-01-01
```

### Fetch multiple posts

```bash
# Fetch latest 20 posts to directory
python main.py --newsletter https://newsletter.substack.com --limit 20 --output-dir ./posts

# Fetch all posts with custom delay
python main.py --newsletter https://newsletter.substack.com --all --output-dir ./posts --delay 3 --jitter 2
```

### Resumable batch fetching (recommended workflow)

For large newsletters, use a two-step process:

```bash
# Step 1: Get the list of all posts (fast, single API call)
python main.py --newsletter https://newsletter.substack.com --list --all --output-dir ./posts

# Step 2: Fetch posts from the list with resume support
python main.py --from-list ./posts/newsletter_posts.json --output-dir ./posts --resume --verbose

# If interrupted, just run the same command again - it skips already downloaded posts
python main.py --from-list ./posts/newsletter_posts.json --output-dir ./posts --resume

# Fetch only posts from a specific date range
python main.py --from-list ./posts/newsletter_posts.json --output-dir ./posts --resume --since 2024-01-01 --until 2024-12-31
```

### Debug network issues

```bash
# Enable verbose output to see what's happening
python main.py --newsletter https://newsletter.substack.com --list --all --verbose
```

## Python API

```python
from src.substack_fetcher import SubstackFetcher

# Initialize (with optional authentication)
fetcher = SubstackFetcher(cookies_path="cookies.json")

# Get a single post with comments
post = fetcher.get_post_with_comments("https://newsletter.substack.com/p/post-slug")
print(post["metadata"]["title"])
print(f"Comments: {post['comment_count']}")

# Get just comments
comments = fetcher.get_comments("https://newsletter.substack.com/p/post-slug")

# List posts from a newsletter
posts = fetcher.get_posts("https://newsletter.substack.com", limit=20)

# Fetch and save multiple posts
fetcher.fetch_all_posts(
    "https://newsletter.substack.com",
    output_dir="./posts",
    limit=50,
    include_comments=True,
    delay=2.0,
    jitter=1.0,
)
```

## Cookie Setup (for paid content)

Create a `cookies.json` file with your session cookies:

```json
[
  {
    "name": "substack.sid",
    "value": "your_session_id",
    "domain": ".substack.com",
    "path": "/",
    "secure": true
  },
  {
    "name": "substack.lli",
    "value": "your_lli_value",
    "domain": ".substack.com",
    "path": "/",
    "secure": true
  },
  {
    "name": "connect.sid",
    "value": "your_connect_sid",
    "domain": ".substack.com",
    "path": "/",
    "secure": true
  }
]
```

### How to get your cookies

1. Open your browser and log into Substack
2. Open Developer Tools (F12 or Cmd+Option+I)
3. Go to **Application** tab (Chrome) or **Storage** tab (Firefox)
4. Expand **Cookies** and select `https://substack.com`
5. Copy the values for the cookies listed above

**Important:** Only use this to access content you're legitimately subscribed to.

## Output Format

### Single post JSON

```json
{
  "metadata": {
    "id": 123456,
    "title": "Post Title",
    "subtitle": "Post subtitle",
    "slug": "post-slug",
    "post_date": "2024-01-15T12:00:00.000Z",
    "audience": "everyone",
    "previous_post_slug": "previous-post",
    "next_post_slug": "next-post"
  },
  "content": "<html content>",
  "is_paywalled": false,
  "comments": [
    {
      "id": 789,
      "body": "Comment text",
      "user": {"name": "Commenter Name"},
      "date": "2024-01-16T10:00:00.000Z"
    }
  ],
  "comment_count": 42
}
```

### Posts list JSON (from `--list`)

```json
{
  "newsletter": "https://newsletter.substack.com",
  "count": 150,
  "posts": [
    {
      "title": "Post Title",
      "slug": "post-slug",
      "url": "https://newsletter.substack.com/p/post-slug",
      "date": "2024-01-15",
      "audience": "everyone",
      "subtitle": "Post subtitle"
    }
  ]
}
```

## Disclaimer

This tool uses unofficial APIs and is not affiliated with or endorsed by Substack. Use responsibly and in compliance with Substack's Terms of Service. Only access content you have legitimate access to.
