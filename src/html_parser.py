"""Parse browser-saved Substack HTML files to extract post content and images."""

from __future__ import annotations

import re
import shutil
from pathlib import Path

from bs4 import BeautifulSoup


def extract_post_id(html_text: str) -> int | None:
    """Extract numeric post ID from the 'saved from url' comment in the HTML.

    Patterns:
      https://substack.com/inbox/post/NNNN
      https://substack.com/home/post/p-NNNN
      https://substack.com/@antiempire/p-NNNN
    """
    m = re.search(
        r'saved from url=\(\d+\)https://substack\.com/(?:home/post/p-|inbox/post/|@[^/]+/p-)(\d+)',
        html_text[:2000],
    )
    return int(m.group(1)) if m else None


def parse_html_file(html_path: Path) -> dict | None:
    """Parse a saved HTML file and return extracted data.

    Returns dict with keys: post_id, title, content_html, images
    or None if parsing fails.
    """
    text = html_path.read_text(encoding='utf-8', errors='replace')

    post_id = extract_post_id(text)

    soup = BeautifulSoup(text, 'html.parser')

    # Extract title from <title> tag
    title_tag = soup.find('title')
    title = title_tag.get_text(strip=True) if title_tag else html_path.stem

    # Find article body
    body_div = soup.find('div', class_='body markup')
    if body_div is None:
        return None

    # Collect image sources before rewriting
    images = []
    for img in body_div.find_all('img'):
        src = img.get('src', '')
        if src and '_files/' in src:
            images.append(src)

    content_html = str(body_div)

    return {
        'post_id': post_id,
        'title': title,
        'content_html': content_html,
        'images': images,
    }


def copy_images(html_path: Path, image_srcs: list[str], post_id: int, dest_base: Path) -> dict[str, str]:
    """Copy article images to dest_base/<post_id>/ and return src->new_path mapping."""
    dest_dir = dest_base / str(post_id)
    dest_dir.mkdir(parents=True, exist_ok=True)

    rewrite_map = {}
    for src in image_srcs:
        # src is like: ./Title_files/uuid_WxH.jpg
        # Resolve relative to the HTML file's directory
        src_path = (html_path.parent / src).resolve()
        if not src_path.exists():
            continue
        dest_file = dest_dir / src_path.name
        if not dest_file.exists():
            shutil.copy2(src_path, dest_file)
        rewrite_map[src] = f'posts/saved/images/{post_id}/{src_path.name}'

    return rewrite_map


def rewrite_image_srcs(content_html: str, rewrite_map: dict[str, str]) -> str:
    """Replace image src attributes in the HTML content."""
    for old_src, new_src in rewrite_map.items():
        content_html = content_html.replace(old_src, new_src)
    return content_html
