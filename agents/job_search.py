"""AI job search -- filter a job list by a natural-language request.

The caller applies the cheap SQL filters first; only the survivors are judged
here, in batches, as compact summaries. Verdicts are cached per (query, job) so
re-filtering or tweaking other filters doesn't re-spend quota on jobs already
judged.
"""

import asyncio
import logging
import threading
from collections import OrderedDict

from llm import gemini

log = logging.getLogger(__name__)

# Large batches keep a full search to ~4 calls, which matters on free-tier RPM.
BATCH_SIZE = 100
MAX_JOBS = 400
CACHE_LIMIT = 20000

PROMPT = """A job seeker describes what they want:

"{query}"

Below is a list of job postings as JSON. Return ONLY the postings that genuinely
fit that request. Be strict: if the request names a specialty, seniority, stack,
or domain, a posting must plausibly satisfy it. Omit anything doubtful.

Return strict JSON:
{{"matches": [{{"id": number, "relevance": 0-100, "reason": "one short sentence"}}]}}

Use only ids from the list. An empty list is a valid answer.

--- POSTINGS ---
{postings}
"""

_cache: "OrderedDict[tuple[str, int], dict | None]" = OrderedDict()
_cache_lock = threading.Lock()


def _normalize(query: str) -> str:
    return " ".join(query.lower().split())


def _cache_get(key):
    with _cache_lock:
        if key in _cache:
            _cache.move_to_end(key)
            return True, _cache[key]
    return False, None


def _cache_put(key, value) -> None:
    with _cache_lock:
        _cache[key] = value
        _cache.move_to_end(key)
        while len(_cache) > CACHE_LIMIT:
            _cache.popitem(last=False)


def _summary(job: dict) -> dict:
    return {
        "id": job["id"],
        "title": job.get("title"),
        "company": job.get("company_name"),
        "location": job.get("location"),
        "department": job.get("department"),
        "remote": job.get("remote_type"),
        "description": (job.get("description") or "")[:300],
    }


async def _judge_batch(query: str, batch: list[dict]) -> dict[int, dict]:
    import json

    payload = await gemini.generate_json(
        PROMPT.format(query=query, postings=json.dumps([_summary(j) for j in batch])),
        priority=gemini.INTERACTIVE,
    )
    matches = payload.get("matches") if isinstance(payload, dict) else payload
    allowed = {j["id"] for j in batch}
    out: dict[int, dict] = {}
    for item in matches or []:
        if not isinstance(item, dict):
            continue
        try:
            job_id = int(item.get("id"))
            relevance = max(0, min(100, int(item.get("relevance", 0))))
        except (TypeError, ValueError):
            continue
        if job_id in allowed:
            out[job_id] = {"relevance": relevance, "reason": str(item.get("reason") or "")[:300]}
    return out


async def ai_filter(query: str, jobs: list[dict]) -> dict:
    """Return {"matches": {job_id: {relevance, reason}}, "judged": n, "truncated": bool}."""
    if not gemini.available():
        raise gemini.LLMUnavailable("GEMINI_API_KEY is not set")

    norm = _normalize(query)
    truncated = len(jobs) > MAX_JOBS
    jobs = jobs[:MAX_JOBS]

    matches: dict[int, dict] = {}
    pending: list[dict] = []
    for job in jobs:
        hit, verdict = _cache_get((norm, job["id"]))
        if not hit:
            pending.append(job)
        elif verdict:
            matches[job["id"]] = verdict

    batches = [pending[i : i + BATCH_SIZE] for i in range(0, len(pending), BATCH_SIZE)]
    results = await asyncio.gather(*(_judge_batch(query, b) for b in batches), return_exceptions=True)

    failures = 0
    for batch, result in zip(batches, results):
        if isinstance(result, BaseException):
            # A rate-limited batch isn't cached, so a retry will judge it.
            failures += 1
            log.warning("ai search batch failed: %s", result)
            continue
        for job in batch:
            verdict = result.get(job["id"])
            _cache_put((norm, job["id"]), verdict)
            if verdict:
                matches[job["id"]] = verdict

    if batches and failures == len(batches):
        first = next(r for r in results if isinstance(r, BaseException))
        raise first if isinstance(first, gemini.LLMError) else gemini.LLMError(str(first))

    return {
        "matches": matches,
        "judged": len(jobs),
        "truncated": truncated,
        "partial": failures > 0,
    }
