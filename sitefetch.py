"""Fetch a company's own web pages as plain text.

Used when Google Search grounding is unavailable: agents summarise only pages
we actually fetched, and those URLs become the stored sources.
"""

import asyncio
import html
import logging
import re
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger(__name__)

HEADERS = {
    "User-Agent": "Mozilla/5.0 (compatible; JobRadar/1.0; personal job search)",
    "Accept": "text/html,application/xhtml+xml",
}
# Server-rendered careers pages run large (figma.com/careers is ~1.7 MB).
MAX_BYTES = 5_000_000
MAX_TEXT_CHARS = 8000

COMPANY_PATHS = ("", "/about", "/about-us", "/company", "/team", "/our-team", "/careers")

_DROP = re.compile(r"<(script|style|noscript|svg|template)[^>]*>.*?</\1>", re.S | re.I)
_TAGS = re.compile(r"<[^>]+>")
_HREF = re.compile(r"""href\s*=\s*["']([^"'#]+)["']""", re.I)


def normalize_website(website: str | None) -> str | None:
    if not website:
        return None
    website = website.strip()
    if not website:
        return None
    if "//" not in website:
        website = f"https://{website}"
    parsed = urlparse(website)
    if not parsed.netloc:
        return None
    return f"{parsed.scheme}://{parsed.netloc}"


def html_to_text(raw: str) -> str:
    text = _DROP.sub(" ", raw)
    text = _TAGS.sub(" ", text)
    text = html.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def extract_links(raw: str, base_url: str) -> list[str]:
    out = []
    for href in _HREF.findall(raw):
        href = html.unescape(href.strip())
        if href.startswith(("javascript:", "tel:")):
            continue
        if not href.startswith("mailto:"):
            href = urljoin(base_url, href)
        if href not in out:
            out.append(href)
    return out


async def fetch_html(client: httpx.AsyncClient, url: str) -> tuple[str, str] | None:
    """Return (final_url, html) or None. Non-HTML and oversized bodies are skipped."""
    try:
        resp = await client.get(url, headers=HEADERS, timeout=15.0, follow_redirects=True)
    except httpx.HTTPError as exc:
        log.debug("fetch failed %s: %s", url, exc)
        return None
    if resp.status_code != 200:
        return None
    if "html" not in resp.headers.get("content-type", ""):
        return None
    if len(resp.content) > MAX_BYTES:
        return None
    return str(resp.url), resp.text


_LD_JSON = re.compile(
    r"""<script[^>]+type\s*=\s*["']application/ld\+json["'][^>]*>(.*?)</script>""", re.S | re.I
)


def _job_posting_description(raw_html: str) -> str | None:
    import json

    for block in _LD_JSON.findall(raw_html):
        try:
            data = json.loads(block.strip())
        except ValueError:
            continue
        items = data if isinstance(data, list) else data.get("@graph", [data]) if isinstance(data, dict) else []
        for item in items:
            if isinstance(item, dict) and item.get("@type") == "JobPosting" and item.get("description"):
                return html_to_text(html.unescape(str(item["description"])))
    return None


async def fetch_job_description(url: str | None) -> str | None:
    """Description for a single posting: JobPosting JSON-LD, else page text.

    Several ATS list endpoints omit descriptions; their hosted job pages
    usually carry JobPosting JSON-LD.
    """
    if not url:
        return None
    async with httpx.AsyncClient() as client:
        result = await fetch_html(client, url)
    if not result:
        return None
    _, raw = result
    description = _job_posting_description(raw)
    if not description:
        text = html_to_text(raw)
        description = text if len(text) > 300 else None
    return description[:MAX_TEXT_CHARS] if description else None


async def fetch_company_pages(website: str | None) -> dict[str, dict]:
    """Fetch the handful of pages that describe a company.

    Returns {final_url: {"text": str, "links": [str]}}, deduplicated by final
    URL so redirects to the homepage aren't counted twice.
    """
    base = normalize_website(website)
    if not base:
        return {}

    async with httpx.AsyncClient() as client:
        results = await asyncio.gather(
            *(fetch_html(client, base + path) for path in COMPANY_PATHS), return_exceptions=True
        )

    pages: dict[str, dict] = {}
    for result in results:
        if not isinstance(result, tuple):
            continue
        final_url, raw = result
        if final_url in pages:
            continue
        text = html_to_text(raw)[:MAX_TEXT_CHARS]
        if len(text) < 200:
            continue
        pages[final_url] = {"text": text, "links": extract_links(raw, final_url)}
    return pages
