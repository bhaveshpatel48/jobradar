"""Gemini wrapper for the agent layer.

Two constraints drive this module's shape:

1. Google Search grounding and structured-JSON output cannot be combined in one
   `generate_content` call on Gemini 2.x -- the server rejects it with a 400. So
   `grounded_json` runs search first, then extracts JSON from that text in a
   second, tool-free call. This also works on 3.x, where the combination is
   allowed but grounding metadata is reported to come back empty.
2. Grounding chunks expose a vertexaisearch.cloud.google.com redirect, not the
   publisher URL, so citations are resolved via HEAD before being stored.
"""

import asyncio
import json
import logging
import re

import httpx

from config import GEMINI_API_KEY, GEMINI_MODEL

log = logging.getLogger(__name__)

_client = None


class LLMUnavailable(RuntimeError):
    """Raised when no API key is configured."""


def available() -> bool:
    return bool(GEMINI_API_KEY)


def _get_client():
    global _client
    if _client is None:
        if not GEMINI_API_KEY:
            raise LLMUnavailable("GEMINI_API_KEY is not set")
        from google import genai

        _client = genai.Client(api_key=GEMINI_API_KEY)
    return _client


def _types():
    from google.genai import types

    return types


_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.MULTILINE)


def parse_json(text: str):
    """Pull a JSON value out of a model response, tolerating code fences."""
    if not text:
        raise ValueError("empty response")
    cleaned = _FENCE.sub("", text).strip()
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass
    # Fall back to the outermost {...} or [...] span.
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON found in response: {cleaned[:200]}")


async def _resolve_redirect(client: httpx.AsyncClient, url: str) -> str | None:
    """Grounding chunks cite a Google redirect; follow it to the real page."""
    try:
        resp = await client.head(url, follow_redirects=True, timeout=10.0)
        final = str(resp.url)
        if "vertexaisearch.cloud.google.com" in final:
            return None
        return final
    except httpx.HTTPError as exc:
        log.debug("could not resolve citation %s: %s", url, exc)
        return None


async def _extract_sources(response) -> list[str]:
    candidates = getattr(response, "candidates", None) or []
    if not candidates:
        return []
    meta = getattr(candidates[0], "grounding_metadata", None)
    chunks = getattr(meta, "grounding_chunks", None) or [] if meta else []

    redirects = []
    for chunk in chunks:
        web = getattr(chunk, "web", None)
        uri = getattr(web, "uri", None) if web else None
        if uri:
            redirects.append(uri)

    if not redirects:
        return []

    async with httpx.AsyncClient() as client:
        resolved = await asyncio.gather(
            *(_resolve_redirect(client, u) for u in redirects), return_exceptions=True
        )

    out: list[str] = []
    for item in resolved:
        if isinstance(item, str) and item not in out:
            out.append(item)
    return out


async def generate_text(prompt: str, *, grounded: bool = False) -> tuple[str, list[str]]:
    """Return (text, sources). Sources are only populated when grounded."""
    client = _get_client()
    types = _types()

    config = None
    if grounded:
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())]
        )

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL, contents=prompt, config=config
    )
    text = getattr(response, "text", "") or ""
    sources = await _extract_sources(response) if grounded else []
    return text, sources


async def generate_json(prompt: str):
    """Structured output, no tools. Safe to use response_mime_type here."""
    client = _get_client()
    types = _types()

    response = await client.aio.models.generate_content(
        model=GEMINI_MODEL,
        contents=prompt,
        config=types.GenerateContentConfig(response_mime_type="application/json"),
    )
    return parse_json(getattr(response, "text", "") or "")


async def grounded_json(research_prompt: str, extract_instruction: str) -> tuple[object, list[str]]:
    """Grounded research, then JSON extraction, as two calls.

    Returns (parsed_json, sources). Kept as two calls because Gemini 2.x rejects
    grounding+JSON in a single request, and 3.x drops grounding metadata when
    they are combined -- either way we would lose the citations.
    """
    findings, sources = await generate_text(research_prompt, grounded=True)
    if not findings.strip():
        return None, sources

    payload = await generate_json(
        f"{extract_instruction}\n\n--- RESEARCH NOTES ---\n{findings}"
    )
    return payload, sources
