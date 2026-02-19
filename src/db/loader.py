"""Load Substack JSON data into PostgreSQL."""

from __future__ import annotations

import re
from html.parser import HTMLParser

from .link_classifier import extract_links_from_html, extract_links_from_text


def strip_html(html: str) -> str:
    """Convert HTML to plain text."""
    if not html:
        return ""
    extractor = _HTMLTextExtractor()
    extractor.feed(html)
    text = "".join(extractor._pieces)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


class _HTMLTextExtractor(HTMLParser):
    def __init__(self):
        super().__init__()
        self._pieces = []
        self._skip = False

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self._skip = True
        elif tag in ("p", "br", "div", "h1", "h2", "h3", "h4", "li", "blockquote"):
            self._pieces.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self._skip = False

    def handle_data(self, data):
        if not self._skip:
            self._pieces.append(data)


class PostLoader:
    """Loads publications, authors, posts, comments, and links into the database."""

    def __init__(self, conn):
        self.conn = conn

    # ── Publications ─────────────────────────────────────────────────────

    def upsert_publication(self, metadata: dict) -> int:
        """Extract publication info from metadata and upsert. Returns publication id."""
        pub_id = metadata['publication_id']

        # Try to find publication details in publishedBylines
        pub_data = self._extract_publication_data(metadata)

        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO substack.publications (id, subdomain, name, custom_domain, logo_url, hero_text)
                VALUES (%(id)s, %(subdomain)s, %(name)s, %(custom_domain)s, %(logo_url)s, %(hero_text)s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    logo_url = EXCLUDED.logo_url,
                    hero_text = EXCLUDED.hero_text
                RETURNING id
            """, pub_data)
            return cur.fetchone()[0]

    def _extract_publication_data(self, metadata: dict) -> dict:
        """Pull publication details from the nested byline structure."""
        pub_id = metadata['publication_id']
        canonical = metadata.get('canonical_url', '')

        # Default: derive subdomain from canonical_url
        subdomain = canonical.split('//')[1].split('.')[0] if '//' in canonical else 'unknown'
        result = {
            'id': pub_id,
            'subdomain': subdomain,
            'name': subdomain,
            'custom_domain': None,
            'logo_url': None,
            'hero_text': None,
        }

        # Try to get richer data from publishedBylines -> publicationUsers -> publication
        for byline in metadata.get('publishedBylines', []):
            for pu in byline.get('publicationUsers', []):
                pub = pu.get('publication', {})
                if pub and pub.get('id') == pub_id:
                    result.update({
                        'subdomain': pub.get('subdomain', subdomain),
                        'name': pub.get('name', subdomain),
                        'custom_domain': pub.get('custom_domain'),
                        'logo_url': pub.get('logo_url'),
                        'hero_text': pub.get('hero_text'),
                    })
                    return result

        return result

    # ── Authors ──────────────────────────────────────────────────────────

    def upsert_author(self, metadata: dict) -> int | None:
        """Extract primary author from metadata and upsert. Returns author id."""
        bylines = metadata.get('publishedBylines', [])
        if not bylines:
            return None

        b = bylines[0]
        author_id = b.get('id')
        if not author_id:
            return None

        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO substack.authors (id, name, handle, photo_url, bio, twitter_screen_name, bestseller_tier)
                VALUES (%s, %s, %s, %s, %s, %s, %s)
                ON CONFLICT (id) DO UPDATE SET
                    name = EXCLUDED.name,
                    handle = EXCLUDED.handle,
                    photo_url = EXCLUDED.photo_url,
                    bio = EXCLUDED.bio
                RETURNING id
            """, (
                author_id,
                b.get('name', 'Unknown'),
                b.get('handle'),
                b.get('photo_url'),
                b.get('bio'),
                b.get('twitter_screen_name'),
                b.get('bestseller_tier'),
            ))
            return cur.fetchone()[0]

    # ── Posts ────────────────────────────────────────────────────────────

    def upsert_post(self, metadata: dict, content_html: str, is_paywalled: bool,
                    pub_id: int, author_id: int | None) -> int:
        """Upsert a post. Returns post id."""
        post_id = metadata['id']
        reactions = metadata.get('reactions')

        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO substack.posts (
                    id, publication_id, primary_author_id, slug, title, subtitle,
                    canonical_url, post_date, updated_at, type, audience, is_paywalled,
                    wordcount, restacks, comment_count, cover_image, description,
                    reactions, content_html, content_text
                ) VALUES (
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s,
                    %s::jsonb, %s, %s
                )
                ON CONFLICT (id) DO UPDATE SET
                    title = EXCLUDED.title,
                    subtitle = EXCLUDED.subtitle,
                    updated_at = EXCLUDED.updated_at,
                    is_paywalled = EXCLUDED.is_paywalled,
                    wordcount = EXCLUDED.wordcount,
                    restacks = EXCLUDED.restacks,
                    comment_count = EXCLUDED.comment_count,
                    reactions = EXCLUDED.reactions,
                    content_html = EXCLUDED.content_html,
                    content_text = EXCLUDED.content_text,
                    loaded_at = NOW()
                RETURNING id
            """, (
                post_id, pub_id, author_id,
                metadata.get('slug', ''),
                metadata.get('title', 'Untitled'),
                metadata.get('subtitle'),
                metadata.get('canonical_url', ''),
                metadata.get('post_date'),
                metadata.get('updated_at'),
                metadata.get('type', 'newsletter'),
                metadata.get('audience'),
                is_paywalled,
                metadata.get('wordcount'),
                metadata.get('restacks', 0),
                metadata.get('comment_count', 0),
                metadata.get('cover_image'),
                metadata.get('description'),
                _json_or_none(reactions),
                content_html,
                strip_html(content_html),
            ))
            return cur.fetchone()[0]

    # ── Tags ─────────────────────────────────────────────────────────────

    def insert_tags(self, post_id: int, metadata: dict):
        """Insert post tags from metadata.postTags."""
        tags = metadata.get('postTags', [])
        if not tags:
            return
        with self.conn.cursor() as cur:
            # Clear existing tags for this post
            cur.execute("DELETE FROM substack.post_tags WHERE post_id = %s", (post_id,))
            for t in tags:
                tag_name = t.get('name') if isinstance(t, dict) else str(t)
                if tag_name:
                    cur.execute(
                        "INSERT INTO substack.post_tags (post_id, tag) VALUES (%s, %s) ON CONFLICT DO NOTHING",
                        (post_id, tag_name)
                    )

    # ── Post links ───────────────────────────────────────────────────────

    def insert_post_links(self, post_id: int, html: str):
        """Extract links from post HTML and insert them."""
        links = extract_links_from_html(html)
        if not links:
            return 0

        with self.conn.cursor() as cur:
            # Clear existing links for re-loads
            cur.execute("DELETE FROM substack.post_links WHERE post_id = %s", (post_id,))
            for lk in links:
                cur.execute("""
                    INSERT INTO substack.post_links
                        (post_id, url, domain, source_category, position_in_post, anchor_text)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    post_id, lk['url'], lk['domain'],
                    lk['source_category'], lk['position_in_post'], lk['anchor_text'],
                ))
        return len(links)

    # ── Comments ─────────────────────────────────────────────────────────

    def insert_comments(self, post_id: int, comments: list[dict]):
        """Insert all comments (with children) and their links. Returns count."""
        if not comments:
            return 0

        with self.conn.cursor() as cur:
            # Clear existing comments for this post (cascades to comment_links)
            cur.execute("DELETE FROM substack.comments WHERE post_id = %s", (post_id,))

        count = 0
        for c in comments:
            count += self._insert_comment_tree(post_id, c)
        return count

    def _insert_comment_tree(self, post_id: int, comment: dict) -> int:
        """Recursively insert a comment and its children. Returns count."""
        cid = comment.get('id')
        if not cid:
            return 0

        body = comment.get('body')
        is_deleted = comment.get('deleted', False)
        date = comment.get('date')
        if not date:
            return 0

        # Determine parent from ancestor_path
        ancestor_path = comment.get('ancestor_path', '')
        parent_id = None
        if ancestor_path:
            parts = ancestor_path.split('.')
            parent_id = int(parts[-1]) if parts[-1] else None

        # Determine if valuable
        is_valuable = self._is_valuable_comment(comment)

        reactions = comment.get('reactions')

        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO substack.comments (
                    id, post_id, user_id, author_name, author_handle,
                    body, body_json, ancestor_path, parent_comment_id,
                    date, edited_at, deleted, reactions, reaction_count, is_valuable
                ) VALUES (
                    %s, %s, %s, %s, %s,
                    %s, %s::jsonb, %s, %s,
                    %s, %s, %s, %s::jsonb, %s, %s
                )
                ON CONFLICT (id) DO NOTHING
            """, (
                cid, post_id,
                comment.get('user_id'),
                comment.get('name'),
                comment.get('handle'),
                body,
                _json_or_none(comment.get('body_json')),
                ancestor_path,
                parent_id,
                date,
                comment.get('edited_at'),
                is_deleted,
                _json_or_none(reactions),
                comment.get('reaction_count', 0),
                is_valuable,
            ))

        # Extract links from comment body
        if body and not is_deleted:
            self._insert_comment_links(cid, post_id, body)

        # Recurse into children
        count = 1
        for child in comment.get('children', []):
            count += self._insert_comment_tree(post_id, child)

        return count

    def _insert_comment_links(self, comment_id: int, post_id: int, body: str):
        """Extract links from a comment body and insert them."""
        links = extract_links_from_text(body)
        if not links:
            return

        with self.conn.cursor() as cur:
            for lk in links:
                cur.execute("""
                    INSERT INTO substack.comment_links
                        (comment_id, post_id, url, domain, source_category, anchor_text)
                    VALUES (%s, %s, %s, %s, %s, %s)
                """, (
                    comment_id, post_id,
                    lk['url'], lk['domain'], lk['source_category'], lk['anchor_text'],
                ))

    def _is_valuable_comment(self, comment: dict) -> bool:
        """Heuristic: flag a comment as valuable when it likely adds substance."""
        if comment.get('deleted', False):
            return False

        body = comment.get('body') or ''
        reaction_count = comment.get('reaction_count', 0)
        links = extract_links_from_text(body)
        link_count = len(links)

        # 2+ external links
        if link_count >= 2:
            return True
        # High engagement
        if reaction_count >= 10:
            return True
        # Substantive text with at least one link
        if len(body) > 500 and link_count >= 1:
            return True

        return False

    # ── Load status tracking ─────────────────────────────────────────────

    def is_loaded(self, file_path: str) -> bool:
        """Check if a file was already successfully loaded."""
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM substack.load_status WHERE file_path = %s AND status = 'success'",
                (file_path,)
            )
            return cur.fetchone() is not None

    def record_status(self, file_path: str, post_id: int | None, status: str, error: str | None = None):
        """Record load status for a file."""
        with self.conn.cursor() as cur:
            cur.execute("""
                INSERT INTO substack.load_status (file_path, post_id, loaded_at, status, error_message)
                VALUES (%s, %s, NOW(), %s, %s)
                ON CONFLICT (file_path) DO UPDATE SET
                    post_id = EXCLUDED.post_id,
                    loaded_at = EXCLUDED.loaded_at,
                    status = EXCLUDED.status,
                    error_message = EXCLUDED.error_message
            """, (file_path, post_id, status, error))


# ── Helpers ──────────────────────────────────────────────────────────────────

def _json_or_none(val) -> str | None:
    """Convert a dict/list to JSON string for JSONB columns, or None."""
    if val is None:
        return None
    import json
    return json.dumps(val)
