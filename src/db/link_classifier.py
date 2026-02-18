"""Extract and classify links from HTML content and plain text."""

import re
from html.parser import HTMLParser
from urllib.parse import urlparse


# ── Domain classification ────────────────────────────────────────────────────

_NEWS_DOMAINS = {
    'nytimes.com', 'wsj.com', 'washingtonpost.com', 'theguardian.com',
    'bbc.co.uk', 'bbc.com', 'reuters.com', 'apnews.com', 'cnn.com',
    'foxnews.com', 'nbcnews.com', 'abcnews.com', 'cbsnews.com',
    'bloomberg.com', 'ft.com', 'economist.com', 'theatlantic.com',
    'politico.com', 'axios.com', 'thehill.com', 'aljazeera.com',
    'rt.com', 'sputnikglobe.com', 'tass.com', 'ria.ru',
    'themoscowtimes.com', 'meduza.io', 'pravda.ru',
}


def classify_domain(domain: str) -> str:
    """Classify a domain into a source category.

    Categories: telegram, substack, youtube, rumble, twitter, archive, blog, news, other
    """
    d = domain.lower().lstrip('www.')

    if 't.me' in d or 'telegram.me' in d or 'telegram.org' in d:
        return 'telegram'
    if 'substack.com' in d:
        return 'substack'
    if 'youtube.com' in d or 'youtu.be' in d:
        return 'youtube'
    if 'rumble.com' in d:
        return 'rumble'
    if d in ('x.com', 'twitter.com') or d.endswith('.x.com') or d.endswith('.twitter.com'):
        return 'twitter'
    if any(a in d for a in ('archive.org', 'archive.ph', 'archive.is', 'archive.today', 'web.archive.org')):
        return 'archive'
    if any(b in d for b in ('blogspot.', 'wordpress.com', 'wordpress.org', 'medium.com', 'livejournal.com')):
        return 'blog'
    if any(d.endswith(n) or d == n for n in _NEWS_DOMAINS):
        return 'news'

    return 'other'


# ── HTML link extraction ─────────────────────────────────────────────────────

class _LinkParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.links: list[dict] = []
        self._pos = 0
        self._in_a = False
        self._anchor_parts: list[str] = []

    def handle_starttag(self, tag, attrs):
        if tag == 'a':
            href = dict(attrs).get('href')
            if href:
                self._in_a = True
                self._anchor_parts = []
                self.links.append({
                    'url': href,
                    'position': self._pos,
                    'anchor_text': None,
                })
                self._pos += 1

    def handle_data(self, data):
        if self._in_a:
            self._anchor_parts.append(data)

    def handle_endtag(self, tag):
        if tag == 'a' and self._in_a:
            if self.links and self.links[-1]['anchor_text'] is None:
                self.links[-1]['anchor_text'] = ''.join(self._anchor_parts).strip() or None
            self._in_a = False


_SKIP_PREFIXES = ('#', 'javascript:', 'mailto:')
_SKIP_DOMAINS = {'substackcdn.com', 'substack-post-media.s3.amazonaws.com'}


def extract_links_from_html(html: str) -> list[dict]:
    """Extract classified links from HTML.

    Returns list of dicts: url, domain, source_category, position_in_post, anchor_text.
    Filters out CDN image URLs and anchors/javascript/mailto.
    """
    parser = _LinkParser()
    parser.feed(html)

    results = []
    for link in parser.links:
        url = link['url']
        if any(url.startswith(p) for p in _SKIP_PREFIXES):
            continue
        try:
            parsed = urlparse(url)
            domain = parsed.netloc
            if not domain:
                continue
            # Skip Substack CDN image URLs
            bare = domain.lower().lstrip('www.')
            if bare in _SKIP_DOMAINS:
                continue
            results.append({
                'url': url,
                'domain': domain,
                'source_category': classify_domain(domain),
                'position_in_post': link['position'],
                'anchor_text': link['anchor_text'],
            })
        except Exception:
            continue

    return results


# ── Plain-text link extraction (for comment bodies) ─────────────────────────

_URL_RE = re.compile(r'https?://[^\s<>"\')\]},]+')


def extract_links_from_text(text: str) -> list[dict]:
    """Extract classified links from plain text (comment bodies).

    Returns list of dicts: url, domain, source_category, anchor_text (always None).
    """
    if not text:
        return []

    results = []
    seen = set()
    for match in _URL_RE.finditer(text):
        url = match.group().rstrip('.')
        if url in seen:
            continue
        seen.add(url)
        try:
            parsed = urlparse(url)
            domain = parsed.netloc
            if not domain:
                continue
            bare = domain.lower().lstrip('www.')
            if bare in _SKIP_DOMAINS:
                continue
            results.append({
                'url': url,
                'domain': domain,
                'source_category': classify_domain(domain),
                'anchor_text': None,
            })
        except Exception:
            continue

    return results
