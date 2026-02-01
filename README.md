# Substack Article Fetcher

Fetch Substack articles and comments programmatically. Supports authenticated access for paid subscriber content.

## Installation

```bash
# Create virtual environment
python -m venv venv
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

## Usage

### Fetch a single post

```bash
# Output to stdout
python main.py --url https://newsletter.substack.com/p/post-slug

# Save to file
python main.py --url https://newsletter.substack.com/p/post-slug --output post.json

# Without comments
python main.py --url https://newsletter.substack.com/p/post-slug --no-comments
```

### Fetch multiple posts from a newsletter

```bash
# Fetch latest 10 posts
python main.py --newsletter https://newsletter.substack.com

# Fetch 50 posts to custom directory
python main.py --newsletter https://newsletter.substack.com --limit 50 --output-dir ./my-posts

# List posts without downloading
python main.py --newsletter https://newsletter.substack.com --list --limit 100
```

### Python API

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

Posts are saved as JSON with the following structure:

```json
{
  "metadata": {
    "id": 123456,
    "title": "Post Title",
    "subtitle": "Post subtitle",
    "slug": "post-slug",
    "post_date": "2024-01-15T12:00:00.000Z",
    "audience": "everyone"
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

## Disclaimer

This tool uses unofficial APIs and is not affiliated with or endorsed by Substack. Use responsibly and in compliance with Substack's Terms of Service. Only access content you have legitimate access to.
