"""Analyze images using LLM vision APIs (Anthropic or Ollama)."""

from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path

SUPPORTED_MODELS = {"haiku", "sonnet"}

# Map friendly names to model IDs
_ANTHROPIC_MODELS = {
    "haiku": "claude-haiku-4-5-20251001",
    "sonnet": "claude-sonnet-4-5-20250929",
}

# ── Prompts ──────────────────────────────────────────────────────────────────

_CONTENT_IMAGE_PROMPT = """\
Analyze this image from a Substack article. Return a JSON object with these fields:

{{
  "description": "A clear 1-3 sentence description of what the image shows.",
  "category": one of: "screenshot", "tweet", "meme", "chart", "table", "infographic", "photo", "map", "document", "other",
  "data": {{
    "extracted_text": "If the image contains readable text (screenshot, tweet, meme text, document), transcribe ALL of it verbatim here. If no significant text, use null.",
    "entities": ["List of named entities: people, organizations, places, mentioned in the image or its text"],
    "chart_description": "If category is chart/table/infographic, describe the data being shown. Otherwise null.",
    "chart_data": "If category is chart/table, extract key data points as structured object. Otherwise null.",
    "source": "If the image shows content from another source (tweet author, news outlet, website), identify it. Otherwise null.",
    "date_depicted": "If a date is visible or clearly referenced in the image, put it here as YYYY-MM-DD. Otherwise null."
  }}
}}

Context from the article:
- Article title: {post_title}
- Image alt text: {alt_text}
- Image caption: {caption}

Return ONLY valid JSON, no markdown fences or explanation."""

_COVER_IMAGE_PROMPT = """\
This is a cover/header image for a Substack article titled "{post_title}".
Describe what the image shows in 1-2 sentences. Return ONLY valid JSON:

{{
  "description": "Your description here.",
  "category": one of: "photo", "illustration", "screenshot", "chart", "map", "other",
  "data": null
}}

Return ONLY valid JSON, no markdown fences or explanation."""


# ── Main entry point ─────────────────────────────────────────────────────────

def analyze_image(
    image_path: str,
    model: str = "haiku",
    is_cover: bool = False,
    context: dict | None = None,
) -> dict:
    """Analyze an image and return structured result.

    Returns dict with keys: description, category, data (dict or None).
    """
    ctx = context or {}
    if is_cover:
        prompt = _COVER_IMAGE_PROMPT.format(
            post_title=ctx.get("post_title") or "Unknown",
        )
    else:
        prompt = _CONTENT_IMAGE_PROMPT.format(
            post_title=ctx.get("post_title") or "Unknown",
            alt_text=ctx.get("alt_text") or "None",
            caption=ctx.get("caption") or "None",
        )

    # Pass 2: add prior analysis context for deeper extraction
    if ctx.get("prior_description"):
        prompt += f"""

A previous (faster) model analyzed this image and produced:
- Category: {ctx.get('prior_category', 'unknown')}
- Description: {ctx['prior_description']}

You are doing a DEEP pass. Focus on:
- Transcribing ALL visible text completely and verbatim
- Extracting structured data from charts/tables
- Identifying all entities (people, organizations, sources)
- Getting exact dates if visible"""

    if model.startswith("ollama/"):
        ollama_model = model.split("/", 1)[1]
        raw = _call_ollama(image_path, prompt, ollama_model)
    elif model in _ANTHROPIC_MODELS:
        raw = _call_anthropic(image_path, prompt, _ANTHROPIC_MODELS[model])
    else:
        raise ValueError(f"Unsupported model: {model}")

    return _parse_response(raw)


# ── Anthropic ────────────────────────────────────────────────────────────────

def _call_anthropic(image_path: str, prompt: str, model_id: str) -> str:
    """Call Anthropic vision API."""
    import anthropic

    client = anthropic.Anthropic()
    image_data = Path(image_path).read_bytes()
    media_type = _get_media_type(image_path)

    message = client.messages.create(
        model=model_id,
        max_tokens=1500,
        messages=[{
            "role": "user",
            "content": [
                {
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": base64.b64encode(image_data).decode(),
                    },
                },
                {
                    "type": "text",
                    "text": prompt,
                },
            ],
        }],
    )

    return message.content[0].text


# ── Ollama ───────────────────────────────────────────────────────────────────

def _call_ollama(image_path: str, prompt: str, model: str) -> str:
    """Call Ollama vision API."""
    import requests

    image_data = Path(image_path).read_bytes()
    b64 = base64.b64encode(image_data).decode()

    resp = requests.post(
        "http://localhost:11434/api/generate",
        json={
            "model": model,
            "prompt": prompt,
            "images": [b64],
            "stream": False,
        },
        timeout=120,
    )
    resp.raise_for_status()
    return resp.json()["response"]


# ── Helpers ──────────────────────────────────────────────────────────────────

def _get_media_type(image_path: str) -> str:
    """Get MIME type by inspecting the file's magic bytes."""
    data = Path(image_path).read_bytes()[:16]
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:4] == b"GIF8":
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    # Fallback to extension
    ext = Path(image_path).suffix.lower()
    return {
        ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
        ".png": "image/png", ".gif": "image/gif",
        ".webp": "image/webp", ".svg": "image/svg+xml",
    }.get(ext, "image/jpeg")


def _parse_response(raw: str) -> dict:
    """Parse LLM response into structured dict."""
    # Strip markdown fences if present
    text = raw.strip()
    if text.startswith("```"):
        text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

    try:
        result = json.loads(text)
    except json.JSONDecodeError:
        # Try to find JSON in the response
        start = text.find("{")
        end = text.rfind("}") + 1
        if start >= 0 and end > start:
            result = json.loads(text[start:end])
        else:
            return {
                "description": text[:500],
                "category": "other",
                "data": None,
            }

    # Normalize
    return {
        "description": result.get("description", ""),
        "category": result.get("category", "other"),
        "data": result.get("data"),
    }
