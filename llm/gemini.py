"""Gemini wrapper for the agent layer.

Constraints that shape this module:

1. Google Search grounding and structured-JSON output cannot be combined in one
   `generate_content` call on Gemini 2.x (server 400), and on 3.x the
   combination drops grounding metadata. So `grounded_json` searches first,
   then extracts JSON from that text in a second, tool-free call.
2. Grounding chunks cite a vertexaisearch.cloud.google.com redirect, not the
   publisher URL, so citations are resolved via HEAD before being stored.
3. Free-tier keys often have zero grounding quota. A grounding 429 trips a
   circuit breaker and raises GroundingUnavailable so callers can fall back to
   fetching pages directly, instead of hammering the endpoint.
4. The SDK's async client is bound to the event loop it was created on, and the
   API calls `asyncio.run` per request -- so clients are cached per loop.
"""

import asyncio
import json
import logging
import re
import threading
import time

import httpx

from config import GEMINI_API_KEY, GEMINI_INTERACTIVE_RESERVE, GEMINI_MODEL, GEMINI_RPM

log = logging.getLogger(__name__)

GROUNDING_COOLDOWN_SECONDS = 30 * 60
BACKGROUND = "background"
INTERACTIVE = "interactive"
# A user is waiting on interactive calls; fail fast rather than hang the request.
INTERACTIVE_MAX_WAIT_SECONDS = 45


class LLMError(RuntimeError):
    """Any failure talking to Gemini that callers should degrade on."""


class LLMUnavailable(LLMError):
    """No API key configured."""


class LLMRateLimited(LLMError):
    """Quota/rate limit on a plain (non-grounded) call -- retry later."""


class LLMQuotaExhausted(LLMRateLimited):
    """The daily request quota is spent. `reset_at` is a unix timestamp."""

    def __init__(self, message: str, reset_at: float):
        super().__init__(message)
        self.reset_at = reset_at


class GroundingUnavailable(LLMError):
    """Google Search grounding is not usable on this key right now."""


_clients: dict[int, object] = {}
_clients_lock = threading.Lock()

_grounding_blocked_until = 0.0


def available() -> bool:
    return bool(GEMINI_API_KEY)


def grounding_available() -> bool:
    return available() and time.time() >= _grounding_blocked_until


def _get_client():
    if not GEMINI_API_KEY:
        raise LLMUnavailable("GEMINI_API_KEY is not set")
    loop_id = id(asyncio.get_running_loop())
    with _clients_lock:
        client = _clients.get(loop_id)
        if client is None:
            from google import genai

            # Old loops are gone once asyncio.run returns; keep the cache tiny.
            if len(_clients) > 8:
                _clients.clear()
            client = genai.Client(api_key=GEMINI_API_KEY)
            _clients[loop_id] = client
    return client


def _types():
    from google.genai import types

    return types


_QUOTA_KEY = "gemini_daily_quota_reset_at"


def _next_quota_reset() -> float:
    """Gemini API daily quotas reset at midnight US Pacific time."""
    from datetime import datetime, timedelta
    from zoneinfo import ZoneInfo

    pacific = ZoneInfo("America/Los_Angeles")
    now = datetime.now(pacific)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=5, microsecond=0)
    return tomorrow.timestamp()


def quota_reset_at() -> float | None:
    """When the daily quota resets, if it is currently exhausted (any process)."""
    from db import get_conn

    with get_conn() as conn:
        row = conn.execute("SELECT value FROM app_state WHERE key = ?", (_QUOTA_KEY,)).fetchone()
    if not row:
        return None
    reset_at = float(row["value"])
    return reset_at if reset_at > time.time() else None


def _mark_quota_exhausted() -> float:
    from db import get_conn

    reset_at = _next_quota_reset()
    with get_conn() as conn:
        conn.execute(
            "INSERT INTO app_state (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_QUOTA_KEY, str(reset_at)),
        )
    return reset_at


def clear_quota_flag() -> None:
    """Forget a recorded exhaustion -- e.g. after the user raised their quota."""
    from db import get_conn

    with get_conn() as conn:
        conn.execute("DELETE FROM app_state WHERE key = ?", (_QUOTA_KEY,))


def quota_message(reset_at: float) -> str:
    from datetime import datetime

    local = datetime.fromtimestamp(reset_at).astimezone()
    return f"Gemini daily quota is used up; it resets at {local:%H:%M %Z}"


def _try_reserve_slot(priority: str) -> float:
    """Claim a call slot in the shared ledger. Returns 0 on success, else seconds
    until a slot frees up. BEGIN IMMEDIATE serialises claims across processes."""
    from db import transaction

    limit = GEMINI_RPM if priority == INTERACTIVE else max(1, GEMINI_RPM - GEMINI_INTERACTIVE_RESERVE)
    now = time.time()
    with transaction() as conn:
        conn.execute("DELETE FROM llm_calls WHERE ts < ?", (now - 120,))
        rows = conn.execute(
            "SELECT ts FROM llm_calls WHERE ts >= ? ORDER BY ts", (now - 60,)
        ).fetchall()
        if len(rows) < limit:
            conn.execute("INSERT INTO llm_calls (ts, priority) VALUES (?, ?)", (now, priority))
            return 0.0
        # The slot frees when the (len - limit)th-oldest call ages out of the window.
        return max(0.5, rows[len(rows) - limit]["ts"] + 60 - now)


async def _throttle(priority: str) -> None:
    waited = 0.0
    while True:
        wait = await asyncio.to_thread(_try_reserve_slot, priority)
        if wait == 0:
            return
        if priority == INTERACTIVE and waited + wait > INTERACTIVE_MAX_WAIT_SECONDS:
            raise LLMRateLimited("Gemini per-minute quota is busy; try again shortly")
        await asyncio.sleep(min(wait, 5.0))
        waited += min(wait, 5.0)


async def _call(prompt: str, config, *, grounded: bool, priority: str = BACKGROUND):
    global _grounding_blocked_until
    from google.genai import errors

    client = _get_client()
    reset_at = await asyncio.to_thread(quota_reset_at)
    if reset_at:
        # Don't spend a request just to be told no again.
        raise LLMQuotaExhausted(quota_message(reset_at), reset_at)
    await _throttle(priority)
    try:
        return await client.aio.models.generate_content(
            model=GEMINI_MODEL, contents=prompt, config=config
        )
    except errors.APIError as exc:
        code = getattr(exc, "code", None)
        if code == 429 and "PerDay" in str(exc):
            reset_at = await asyncio.to_thread(_mark_quota_exhausted)
            log.warning("Gemini daily quota exhausted; pausing LLM calls until %s", reset_at)
            raise LLMQuotaExhausted(quota_message(reset_at), reset_at) from exc
        if grounded and code in (400, 403, 429):
            _grounding_blocked_until = time.time() + GROUNDING_COOLDOWN_SECONDS
            log.warning("grounding unavailable (%s); falling back for %ss", code, GROUNDING_COOLDOWN_SECONDS)
            raise GroundingUnavailable(str(exc)) from exc
        if code == 429:
            raise LLMRateLimited(str(exc)) from exc
        raise LLMError(f"Gemini API error {code}: {exc}") from exc
    except httpx.HTTPError as exc:
        raise LLMError(f"network error talking to Gemini: {exc}") from exc


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
    for opener, closer in (("{", "}"), ("[", "]")):
        start, end = cleaned.find(opener), cleaned.rfind(closer)
        if start != -1 and end > start:
            try:
                return json.loads(cleaned[start : end + 1])
            except json.JSONDecodeError:
                continue
    raise ValueError(f"no JSON found in response: {cleaned[:200]}")


async def _resolve_redirect(client: httpx.AsyncClient, url: str) -> str | None:
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
    chunks = (getattr(meta, "grounding_chunks", None) or []) if meta else []

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
    types = _types()
    config = None
    if grounded:
        if not grounding_available():
            raise GroundingUnavailable("grounding disabled by circuit breaker")
        config = types.GenerateContentConfig(
            tools=[types.Tool(google_search=types.GoogleSearch())],
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),
        )
    response = await _call(prompt, config, grounded=grounded)
    text = getattr(response, "text", "") or ""
    sources = await _extract_sources(response) if grounded else []
    return text, sources


async def generate_json(prompt: str, *, priority: str = BACKGROUND):
    """Structured output, no tools. Pass priority=INTERACTIVE when a user is waiting."""
    types = _types()
    response = await _call(
        prompt,
        types.GenerateContentConfig(response_mime_type="application/json"),
        grounded=False,
        priority=priority,
    )
    return parse_json(getattr(response, "text", "") or "")


async def grounded_json(research_prompt: str, extract_instruction: str) -> tuple[object, list[str]]:
    """Grounded research, then JSON extraction, as two calls.

    Raises GroundingUnavailable when search can't be used, so the caller can
    fall back to fetched pages.
    """
    findings, sources = await generate_text(research_prompt, grounded=True)
    if not findings.strip():
        return None, sources
    payload = await generate_json(f"{extract_instruction}\n\n--- RESEARCH NOTES ---\n{findings}")
    return payload, sources


async def json_from_pages(pages: dict[str, str], extract_instruction: str) -> object:
    """Extract JSON from pages we fetched ourselves. `pages` maps URL -> text."""
    corpus = "\n\n".join(f"--- PAGE: {url} ---\n{text}" for url, text in pages.items())
    return await generate_json(f"{extract_instruction}\n\n{corpus}")
